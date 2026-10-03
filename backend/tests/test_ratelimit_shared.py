"""The Redis-backed limiter. Tests that need a real Redis run when TEST_REDIS_URL is set (CI does; locally use docker)."""
import os
import threading
import time

import pytest

REDIS = os.environ.get("TEST_REDIS_URL", "")
needs_redis = pytest.mark.skipif(not REDIS, reason="set TEST_REDIS_URL to run against a real Redis")


def test_the_limiter_is_chosen_by_configuration(monkeypatch):
    from app import ratelimit
    from app.config import settings
    assert isinstance(ratelimit.make_limiter(), ratelimit.SlidingWindowLimiter)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    assert isinstance(ratelimit.make_limiter(), ratelimit.RedisLimiter)


def test_an_unreachable_redis_fails_open_instead_of_taking_the_app_down(monkeypatch, caplog):
    from app import ratelimit
    limiter = ratelimit.RedisLimiter("redis://127.0.0.1:1/0")
    assert [limiter.check("k", 1) for _ in range(3)] == [None, None, None]
    assert "allowing the request" in caplog.text


@needs_redis
def test_redis_limiter_counts_and_slides():
    from app import ratelimit
    limiter = ratelimit.RedisLimiter(REDIS)
    limiter.reset()
    assert [limiter.check("slide", 2, window=0.5) for _ in range(2)] == [None, None]
    wait = limiter.check("slide", 2, window=0.5)
    assert wait is not None and 0 < wait <= 0.5
    time.sleep(0.6)
    assert limiter.check("slide", 2, window=0.5) is None
    assert all(limiter.check("unlimited", 0) is None for _ in range(50))
    limiter.reset()


@needs_redis
def test_two_limiters_share_one_count_as_two_processes_would():
    from app import ratelimit
    first, second = ratelimit.RedisLimiter(REDIS), ratelimit.RedisLimiter(REDIS)
    first.reset()
    assert first.check("shared", 3) is None and second.check("shared", 3) is None and first.check("shared", 3) is None
    assert second.check("shared", 3) is not None            # the fourth request is refused, whichever process sends it
    first.reset()


@needs_redis
def test_the_check_is_atomic_under_concurrency():
    from app import ratelimit
    limiter = ratelimit.RedisLimiter(REDIS)
    limiter.reset()
    allowed, barrier = [], threading.Barrier(20)

    def hit():
        barrier.wait()
        allowed.append(limiter.check("race", 5) is None)
    threads = [threading.Thread(target=hit) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert allowed.count(True) == 5                         # exactly the limit gets through, never more
    limiter.reset()
