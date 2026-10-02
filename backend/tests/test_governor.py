import uuid

import httpx
import pytest

from app.config import settings
from app.reliability.circuit_breaker import CircuitOpen
from app.reliability.governor import Governor, QuotaExhausted
from app.reliability.hosted import hosted_call, reset_breaker, set_governor


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.t = start
        self.slept: list[float] = []

    def clock(self):
        return self.t

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def provider(monkeypatch):
    """A provider name unique to the test, with limits set through the same override path users use."""
    name = f"p_{uuid.uuid4().hex[:8]}"

    def configure(**limits):
        monkeypatch.setattr(settings, "quota_overrides", {name: limits})
        return name

    configure.name = name
    return configure


async def test_a_full_minute_window_waits_for_the_next_minute(clock, provider):
    name = provider(requests={"minute": 2})
    governor = Governor(clock=clock.clock, sleep=clock.sleep)
    for _ in range(2):
        await governor.acquire(name)
    assert clock.slept == []
    await governor.acquire(name)                         # third call in the same minute
    assert len(clock.slept) == 1 and 0 < clock.slept[0] <= 61
    assert governor.remaining(name) == 1


async def test_an_exhausted_daily_limit_raises_with_the_reset_time(clock, provider):
    name = provider(requests={"day": 3})
    governor = Governor(clock=clock.clock, sleep=clock.sleep)
    for _ in range(3):
        await governor.acquire(name)
    with pytest.raises(QuotaExhausted) as raised:
        await governor.acquire(name)
    assert raised.value.window == "day" and raised.value.unit == "requests" and 0 < raised.value.resets_in <= 86400
    assert "resets in" in str(raised.value) and clock.slept == []   # a day is never waited out


async def test_units_are_counted_and_a_request_larger_than_the_window_can_never_succeed(clock, provider):
    name = provider(pages={"minute": 30})
    governor = Governor(clock=clock.clock, sleep=clock.sleep)
    await governor.acquire(name, pages=20)
    assert governor.remaining(name, "pages") == 10
    with pytest.raises(QuotaExhausted) as raised:
        await governor.acquire(name, pages=31)
    assert raised.value.resets_in == float("inf")
    assert governor.max_units(name, "pages") == 30        # callers batch to this size


async def test_counters_survive_a_restart_and_reset_when_the_window_rolls_over(clock, provider):
    name = provider(requests={"day": 2})
    first = Governor(clock=clock.clock, sleep=clock.sleep)
    await first.acquire(name)
    await first.acquire(name)
    restarted = Governor(clock=clock.clock, sleep=clock.sleep)      # a new process: counters come from the database
    with pytest.raises(QuotaExhausted):
        await restarted.acquire(name)
    clock.t += 86400
    await restarted.acquire(name)                                    # next UTC day
    assert restarted.remaining(name) == 1


async def test_a_provider_cooldown_is_waited_out_when_short_and_raised_when_long(clock, provider):
    name = provider(requests={"minute": 100})
    governor = Governor(clock=clock.clock, sleep=clock.sleep, max_wait=30)
    governor.cooldown(name, 10)
    await governor.acquire(name)
    assert clock.slept == [10]
    governor.cooldown(name, 120)
    with pytest.raises(QuotaExhausted) as raised:
        await governor.acquire(name)
    assert raised.value.window == "cooldown"


async def test_usage_known_only_afterwards_is_recorded_and_snapshot_reports_it(clock, provider):
    name = provider(requests={"minute": 5}, audio_seconds={"day": 1000})
    governor = Governor(clock=clock.clock, sleep=clock.sleep)
    await governor.acquire(name, requests=1, audio_seconds=1)
    governor.record(name, audio_seconds=599)
    assert governor.remaining(name, "audio_seconds") == 400
    entry = next(e for e in governor.snapshot() if e["provider"] == name)
    assert entry["limits"]["audio_seconds"]["day"] == {"used": 600, "limit": 1000}


def test_defaults_include_the_verified_groq_whisper_free_limits():
    limits = Governor().limits_for("groq_whisper")
    assert limits["requests"] == {"minute": 20, "day": 2000}
    assert limits["audio_seconds"] == {"hour": 7200, "day": 28800}


# ---- hosted_call ---------------------------------------------------------------------------

@pytest.fixture
def hosted(clock):
    reset_breaker()
    set_governor(Governor(clock=clock.clock, sleep=clock.sleep))
    yield
    set_governor(None)
    reset_breaker()


def response(status, **headers):
    return httpx.Response(status, headers=headers, request=httpx.Request("POST", "http://provider.test"))


def flaky(*outcomes):
    """A provider call that returns the given outcomes in order: an int is an HTTP status, 'net' is a network error."""
    calls = []

    async def call():
        calls.append(1)
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if outcome == "net":
            raise httpx.ConnectError("boom")
        if outcome != 200:
            response(outcome, **({"retry-after": "2"} if outcome == 429 else {})).raise_for_status()
        return "ok"

    call.calls = calls
    return call


async def test_a_429_with_a_short_retry_after_is_retried_after_waiting(hosted, clock):
    call = flaky(429, 200)
    assert await hosted_call("hosted_a", call) == "ok"
    assert len(call.calls) == 2 and 2 in clock.slept


async def test_a_429_asking_for_a_long_wait_pauses_the_work_instead(hosted):
    async def call():
        response(429, **{"retry-after": "300"}).raise_for_status()

    with pytest.raises(QuotaExhausted) as raised:
        await hosted_call("hosted_b", call)
    assert raised.value.window == "provider" and raised.value.resets_in == 300


async def test_server_errors_and_network_errors_are_retried_a_bounded_number_of_times(hosted):
    assert await hosted_call("hosted_c", flaky(503, "net", 200)) == "ok"
    always_down = flaky(502)
    with pytest.raises(httpx.HTTPStatusError):
        await hosted_call("hosted_d", always_down)
    assert len(always_down.calls) == 3                      # 1 try + 2 retries, then it gives up


async def test_client_errors_are_not_retried_and_do_not_trip_the_breaker(hosted):
    call = flaky(400)
    with pytest.raises(httpx.HTTPStatusError):
        await hosted_call("hosted_e", call)
    assert len(call.calls) == 1


async def test_repeated_failures_open_the_circuit_so_a_dead_provider_is_not_hammered(hosted):
    with pytest.raises(httpx.HTTPStatusError):
        await hosted_call("hosted_f", flaky(500))          # 3 failures; the breaker opens at 5
    with pytest.raises(CircuitOpen):
        await hosted_call("hosted_f", flaky(500))          # failures 4 and 5 open it before the third retry
    healthy = flaky(200)
    with pytest.raises(CircuitOpen):
        await hosted_call("hosted_f", healthy)             # a dead provider is no longer called at all
    assert healthy.calls == []
