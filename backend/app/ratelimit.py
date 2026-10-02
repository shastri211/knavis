"""Sliding-window rate limiting.

It protects the server from a client that sends too much (a runaway script, a password guesser, a free-tier quota
burner). By default counts live in the process, so behind several workers each has its own counts and the real limit is
a multiple of the setting. Set REDIS_URL and all processes share one count (``RedisLimiter``).
"""
import logging
import threading
import time
from collections import defaultdict, deque
from uuid import uuid4

from fastapi import HTTPException, Request

from .config import settings

logger = logging.getLogger("mragrag")


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


_SLIDING_WINDOW = """
local key, now, window, limit, member = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
if redis.call('ZCARD', key) >= limit then
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  return tostring(math.max(0.1, tonumber(oldest[2]) + window - now))
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, math.ceil(window) + 1)
return '0'
"""


class RedisLimiter:
    """The same sliding window, counted in Redis so every process (and host) shares one limit.

    The check is one atomic script. If Redis cannot be reached the request is allowed and the problem is logged: a
    broken counter must not take the whole application down with it.
    """
    PREFIX = "knavis:rl:"

    def __init__(self, url: str):
        import redis
        self._redis = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        self._script = self._redis.register_script(_SLIDING_WINDOW)

    def check(self, key: str, limit: int, window: float = 60.0) -> float | None:
        if limit <= 0:
            return None
        try:
            now = time.time()
            wait = float(self._script(keys=[self.PREFIX + key], args=[now, window, limit, f"{now}:{uuid4().hex}"]))
        except Exception as exc:
            logger.warning("Rate limit store unavailable (%s: %s); allowing the request", type(exc).__name__, str(exc)[:100])
            return None
        return wait if wait > 0 else None

    def reset(self) -> None:
        for key in self._redis.scan_iter(self.PREFIX + "*"):
            self._redis.delete(key)


def make_limiter():
    return RedisLimiter(settings.redis_url) if settings.redis_url else SlidingWindowLimiter()


limiter = make_limiter()


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
