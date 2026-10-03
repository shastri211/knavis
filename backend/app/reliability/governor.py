"""Quota governor: keeps hosted free-tier usage inside the limits, persistently.

Counters live in SQLite, so a restart does not forget what was already spent today. A call that
would exceed a per-minute limit waits for the window to roll over; one that would exceed an
hourly/daily limit (or needs a long wait) raises ``QuotaExhausted`` so the caller can pause the
work and resume later instead of failing or burning the quota.

The built-in limits are deliberately conservative placeholders: free-tier numbers differ per
account and change often. Set your account's real values with QUOTA_OVERRIDES. A provider's own
429 response stays authoritative: it puts the provider on a cooldown regardless of these counts.
"""
import asyncio
import time
from collections.abc import Callable

from ..config import settings
from ..db import SessionLocal
from ..models import QuotaUsage

WINDOW_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}   # day = UTC day

# provider -> unit -> window -> limit
DEFAULT_LIMITS: dict[str, dict[str, dict[str, int]]] = {
    # Verified against Groq's rate-limit docs (free plan): 20 RPM, 2K RPD, 7.2K audio s/hour, 28.8K audio s/day.
    "groq_whisper": {"requests": {"minute": 20, "day": 2000}, "audio_seconds": {"hour": 7200, "day": 28800}},
    # Conservative placeholders: real limits depend on the model and the account.
    "groq_chat": {"requests": {"minute": 20, "day": 1000}},
    "groq_vision": {"requests": {"minute": 15, "day": 500}},
    "nvidia_chat": {"requests": {"minute": 30}},
    "nvidia_embed": {"requests": {"minute": 30}},
    "nvidia_ocr": {"requests": {"minute": 30}},
    "mistral_ocr": {"pages": {"minute": 30, "day": 500}, "requests": {"minute": 20}},
    "gemini_vision": {"requests": {"minute": 8, "day": 200}},
    "assemblyai": {"requests": {"minute": 20, "day": 200}},
    "openrouter_chat": {"requests": {"minute": 20, "day": 50}},
}


class QuotaExhausted(RuntimeError):
    """A limit is used up. ``resets_in`` is the number of seconds until it is expected to roll over."""

    def __init__(self, provider: str, unit: str, window: str, resets_in: float):
        self.provider, self.unit, self.window, self.resets_in = provider, unit, window, resets_in
        super().__init__(f"{provider}: the {window} limit for {unit} is used up (resets in about {format_wait(resets_in)}).")


def format_wait(seconds: float) -> str:
    if seconds == float("inf"):
        return "never: the request is larger than the limit allows"
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds / 3600:.1f} h"


class Governor:
    def __init__(self, *, clock: Callable[[], float] = time.time, sleep=asyncio.sleep, max_wait: float = 30.0):
        self.clock, self.sleep, self.max_wait = clock, sleep, max_wait
        self._cooldowns: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---- configuration ------------------------------------------------------------------

    def limits_for(self, provider: str) -> dict[str, dict[str, int]]:
        merged = {unit: dict(windows) for unit, windows in DEFAULT_LIMITS.get(provider, {}).items()}
        for unit, windows in (settings.quota_overrides.get(provider) or {}).items():
            merged.setdefault(unit, {}).update(windows)
        return merged

    def max_units(self, provider: str, unit: str) -> int | None:
        """The most ``unit`` that one request can ask for (the smallest limit of that unit), for batching."""
        windows = self.limits_for(provider).get(unit, {})
        return min(windows.values()) if windows else None

    # ---- counters -----------------------------------------------------------------------

    def _bucket(self, window: str, now: float) -> int:
        return int(now // WINDOW_SECONDS[window])

    def _used(self, db, provider: str, unit: str, window: str, now: float) -> int:
        row = db.get(QuotaUsage, f"{provider}:{unit}:{window}")
        return row.used if row is not None and row.bucket == self._bucket(window, now) else 0

    def _add(self, db, provider: str, units: dict[str, int], now: float) -> None:
        for unit, amount in units.items():
            for window in self.limits_for(provider).get(unit, {}):
                row_id, bucket = f"{provider}:{unit}:{window}", self._bucket(window, now)
                row = db.get(QuotaUsage, row_id)
                if row is None:
                    db.add(QuotaUsage(id=row_id, bucket=bucket, used=max(0, amount)))
                elif row.bucket != bucket:
                    row.bucket, row.used = bucket, max(0, amount)
                else:
                    row.used = max(0, row.used + amount)

    def _blocker(self, db, provider: str, units: dict[str, int], now: float):
        """The first limit this request would exceed, as ``(unit, window, seconds until it resets)``."""
        for unit, need in units.items():
            for window, limit in self.limits_for(provider).get(unit, {}).items():
                if self._used(db, provider, unit, window, now) + need > limit:
                    size = WINDOW_SECONDS[window]
                    resets_in = (self._bucket(window, now) + 1) * size - now
                    # asking for more than the whole window allows can never succeed by waiting
                    return unit, window, (float("inf") if need > limit else resets_in)
        return None

    # ---- public API ---------------------------------------------------------------------

    async def acquire(self, provider: str, *, max_wait: float | None = None, **units: int) -> None:
        """Reserve ``units`` (default one request). Waits up to ``max_wait`` seconds (default: the governor's)
        for a per-minute window; raises ``QuotaExhausted`` otherwise."""
        max_wait = self.max_wait if max_wait is None else max_wait
        units = {u: n for u, n in (units or {"requests": 1}).items() if n > 0}
        lock = self._locks.setdefault(provider, asyncio.Lock())
        async with lock:
            while True:
                now = self.clock()
                cooling = self._cooldowns.get(provider, 0) - now
                if cooling > 0:
                    if cooling > max_wait:
                        raise QuotaExhausted(provider, "requests", "cooldown", cooling)
                    await self.sleep(cooling)
                    continue
                with SessionLocal() as db:
                    blocked = self._blocker(db, provider, units, now)
                    if blocked is None:
                        self._add(db, provider, units, now)
                        db.commit()
                        return
                unit, window, resets_in = blocked
                if window == "minute" and resets_in <= max_wait:
                    await self.sleep(resets_in + 0.05)
                    continue
                raise QuotaExhausted(provider, unit, window, resets_in)

    def record(self, provider: str, **units: int) -> None:
        """Count usage that is only known after the call (e.g. audio seconds); never blocks."""
        with SessionLocal() as db:
            self._add(db, provider, {u: n for u, n in units.items() if n > 0}, self.clock())
            db.commit()

    def adjust(self, provider: str, **delta: int) -> None:
        """Apply a signed correction, e.g. swap an up-front estimate for the real amount."""
        with SessionLocal() as db:
            self._add(db, provider, {u: n for u, n in delta.items() if n}, self.clock())
            db.commit()

    def cooldown(self, provider: str, seconds: float) -> None:
        """Called on a provider 429: hold all calls to it for ``seconds``."""
        self._cooldowns[provider] = max(self._cooldowns.get(provider, 0), self.clock() + seconds)

    def remaining(self, provider: str, unit: str = "requests") -> int | None:
        """What is left in the tightest window, or None when ``unit`` has no limit."""
        windows = self.limits_for(provider).get(unit, {})
        if not windows:
            return None
        now = self.clock()
        with SessionLocal() as db:
            return min(limit - self._used(db, provider, unit, window, now) for window, limit in windows.items())

    def snapshot(self) -> list[dict]:
        now = self.clock()
        out = []
        with SessionLocal() as db:
            for provider in sorted(set(DEFAULT_LIMITS) | set(settings.quota_overrides)):
                units = {}
                for unit, windows in self.limits_for(provider).items():
                    units[unit] = {}
                    for window, limit in windows.items():
                        used = self._used(db, provider, unit, window, now)
                        units[unit][window] = {"used": used, "limit": limit, "remaining": max(0, limit - used)}
                out.append({
                    "provider": provider, "limits": units,
                    "cooldown_seconds": max(0, round(self._cooldowns.get(provider, 0) - now)),
                })
        return out
