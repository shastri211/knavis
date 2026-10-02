"""The one way to call a hosted provider: quota governor + circuit breaker + bounded retries."""
import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx

from .circuit_breaker import CircuitBreaker
from .governor import Governor, QuotaExhausted

T = TypeVar("T")

_governor: Governor | None = None
_breaker = CircuitBreaker(threshold=5, recovery_seconds=30)


def get_governor() -> Governor:
    global _governor
    if _governor is None:
        _governor = Governor()
    return _governor


def set_governor(governor: Governor | None) -> None:
    """Replace the shared governor (tests inject a fake clock; None restores the default)."""
    global _governor
    _governor = governor


def reset_breaker() -> None:
    _breaker.state.clear()


def retry_after_seconds(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


async def hosted_call(
    provider: str,
    call: Callable[[], Awaitable[T]],
    *,
    units: dict[str, int] | None = None,
    retries: int = 2,
    max_wait: float | None = None,
) -> T:
    """Run ``call`` against ``provider`` without exceeding its quota.

    * the governor reserves ``units`` first (default one request), waiting up to ``max_wait`` seconds for a
      per-minute window, and may raise ``QuotaExhausted``;
    * HTTP 429 puts the provider on a cooldown and is retried once the wait is short, otherwise
      it is raised as ``QuotaExhausted`` so the work can pause and resume later;
    * 5xx and network errors count against the circuit breaker and are retried with backoff;
    * other 4xx errors are the caller's mistake and are raised untouched.
    """
    governor = get_governor()
    await governor.acquire(provider, max_wait=max_wait, **(units or {"requests": 1}))
    attempt = 0
    while True:
        _breaker.before_call(provider)
        try:
            result = await call()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                delay = retry_after_seconds(exc.response) or 20.0
                governor.cooldown(provider, delay)
                if attempt < retries and delay <= (max_wait if max_wait is not None else governor.max_wait):
                    attempt += 1
                    await governor.sleep(delay)
                    continue
                raise QuotaExhausted(provider, "requests", "provider", delay) from exc
            if status >= 500:
                _breaker.failure(provider)
                if attempt < retries:
                    attempt += 1
                    await governor.sleep(min(8.0, 0.8 * 2 ** attempt))
                    continue
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            _breaker.failure(provider)
            if attempt < retries:
                attempt += 1
                await governor.sleep(min(8.0, 0.8 * 2 ** attempt))
                continue
            raise
        _breaker.success(provider)
        return result
