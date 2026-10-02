"""A small in-process sliding-window rate limiter.

It protects one server process from a client that sends too much (a runaway script, a password guesser, a
free-tier quota burner). State is per process: behind several workers each has its own counts, so the real limit
is a multiple of the setting; use a shared store (Redis) if the app is ever scaled out.
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from .config import settings


class SlidingWindowLimiter:
    def __init__(self):
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: float = 60.0) -> float | None:
        """Count one request. Returns ``None`` when allowed, otherwise the seconds until it would be."""
        if limit <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return max(0.1, hits[0] + window - now)
            hits.append(now)
            if len(self._hits) > 10_000:   # forget keys that went quiet so the table cannot grow without bound
                for stale in [k for k, v in self._hits.items() if not v or v[-1] <= now - window]:
                    del self._hits[stale]
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def client_address(request: Request) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce(bucket: str, key: str, limit: int) -> None:
    wait = limiter.check(f"{bucket}:{key}", limit)
    if wait is not None:
        raise HTTPException(429, "Too many requests. Please wait a moment and try again.", headers={"Retry-After": str(int(wait) + 1)})
