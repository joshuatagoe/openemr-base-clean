"""Provider-call resilience (app/providers/resilience.py): bounded retry with backoff and the concurrency gate.

Failure modes guarded: a burst of provider 429s degrading briefings within milliseconds while seconds of
budget remain (the 50-user load-test finding); retries overrunning the request timeout; unbounded
concurrency at the provider; non-retryable errors being retried.
"""

from __future__ import annotations

import asyncio

import pytest

from app.providers import resilience
from app.providers.base import ProviderAuthenticationError, ProviderRateLimitError, ProviderUnavailableError
from app.providers.resilience import ProviderGate, backoff_delay, call_with_retry

pytestmark = pytest.mark.anyio


def test_backoff_prefers_retry_after_then_grows_exponentially_with_a_cap() -> None:
    assert backoff_delay(2, 1.5) == 1.5
    assert backoff_delay(5, 0) == 0.0
    for attempt, ceiling in ((2, 0.5), (3, 1.0), (4, 2.0), (5, 4.0), (9, 4.0)):
        for _ in range(20):
            assert 0 <= backoff_delay(attempt, None) <= ceiling, attempt


async def test_rate_limit_is_retried_after_the_providers_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(resilience, "_sleep", fake_sleep)
    attempts: list[int] = []

    async def call(attempt: int) -> str:
        attempts.append(attempt)
        if attempt < 3:
            raise ProviderRateLimitError("r", retry_after=0.25)
        return "ok"

    assert await call_with_retry(call, max_attempts=3, budget_seconds=6.0, stage="extraction") == "ok"
    assert attempts == [1, 2, 3] and waits == [0.25, 0.25]


async def test_budget_bounds_total_waiting_and_the_last_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(resilience, "_sleep", fake_sleep)

    async def always_limited(attempt: int) -> str:
        raise ProviderRateLimitError("r", retry_after=2.0)

    with pytest.raises(ProviderRateLimitError):
        await call_with_retry(always_limited, max_attempts=6, budget_seconds=3.0, stage="extraction")
    assert waits == [2.0]  # a second 2 s wait would exceed the 3 s budget: raise instead of overrunning the timeout


async def test_non_retryable_errors_are_not_retried_and_attempts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(resilience, "_sleep", fake_sleep)
    calls = 0

    async def auth_fails(attempt: int) -> str:
        nonlocal calls
        calls += 1
        raise ProviderAuthenticationError("a")

    with pytest.raises(ProviderAuthenticationError):
        await call_with_retry(auth_fails, max_attempts=5, budget_seconds=6.0, stage="extraction")
    assert calls == 1

    calls = 0

    async def always_down(attempt: int) -> str:
        nonlocal calls
        calls += 1
        raise ProviderUnavailableError("u")

    with pytest.raises(ProviderUnavailableError):
        await call_with_retry(always_down, max_attempts=3, budget_seconds=60.0, stage="turn")
    assert calls == 3


async def test_gate_bounds_in_flight_calls_and_reports_waiting() -> None:
    gate = ProviderGate(concurrency=2)
    in_flight = 0
    peak = 0
    peak_waiting = 0

    async def worker() -> None:
        nonlocal in_flight, peak, peak_waiting
        peak_waiting = max(peak_waiting, gate.waiting)
        async with gate.slot():
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1

    await asyncio.gather(*(worker() for _ in range(8)))
    assert peak == 2 and gate.waiting == 0
    assert gate.concurrency == 2


async def test_gate_releases_the_slot_on_error() -> None:
    gate = ProviderGate(concurrency=1)

    async def failing() -> None:
        async with gate.slot():
            raise ProviderUnavailableError("u")

    with pytest.raises(ProviderUnavailableError):
        await failing()
    async with gate.slot():  # would hang if the failed call had kept the slot
        pass


async def test_gate_degrades_fast_with_busy_instead_of_queueing_into_the_timeout() -> None:
    """The 50-user load-test regression: a gate that queues past the request budget turns a fast, explicit
    rate-limit degrade into a silent timeout. A slot wait is bounded and raises ProviderBusyError."""
    from app.providers.base import ProviderBusyError

    gate = ProviderGate(concurrency=1, max_wait_seconds=0.05)
    async with gate.slot():
        with pytest.raises(ProviderBusyError):
            async with gate.slot():
                pass
    assert gate.rejected == 1 and gate.waiting == 0
    async with gate.slot():  # the slot is free again afterwards
        pass
