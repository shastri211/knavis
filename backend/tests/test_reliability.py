import pytest
from app.reliability.circuit_breaker import CircuitBreaker, CircuitOpen
from app.reliability.limits import RateLimiter

def test_circuit_opens():
    c = CircuitBreaker(threshold=2, recovery_seconds=60)
    c.failure("nvidia"); c.failure("nvidia")
    with pytest.raises(CircuitOpen):
        c.before_call("nvidia")

def test_rate_limiter():
    r = RateLimiter(limit=2, window_seconds=60)
    assert r.allow("x")
    assert r.allow("x")
    assert not r.allow("x")
