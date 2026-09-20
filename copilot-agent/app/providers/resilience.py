"""Provider-call resilience: a process-wide concurrency gate and bounded retry with backoff
(ARCHITECTURE.md section 13, "queue with backpressure"; section 11, provider failures).

Under a burst, the provider's rate limit is the first ceiling. Two mechanisms turn a burst
into a short queue instead of a degraded briefing:

* ``ProviderGate`` - an ``asyncio.Semaphore`` around every model call in this process, so
  at most ``concurrency`` requests are in flight at the provider; the rest wait in-process
  for a slot (hundreds of milliseconds under the load-test burst, zero at clinic load).
* ``call_with_retry`` - retries a retryable ``ProviderError`` with exponential backoff, or
  the provider's ``Retry-After`` when it sent one, while the total wait stays inside a
  budget so the request's own timeout is never the thing that fires. Attempts and delays
  are logged (codes and timings only).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from app.observability import log_event
from app.providers.base import ProviderBusyError, ProviderError

T = TypeVar("T")


class ProviderGate:
    """Bounds concurrent provider calls in this process. Configure once at startup."""

    def __init__(self, concurrency: int = 16, max_wait_seconds: float = 4.0) -> None:
        self._concurrency = concurrency
        self._max_wait_seconds = max_wait_seconds
        self._semaphore = asyncio.Semaphore(concurrency)
        self._waiting = 0
        self.rejected = 0  # calls that found no slot inside the wait budget

    def configure(self, concurrency: int, max_wait_seconds: float | None = None) -> None:
        self._concurrency = concurrency
        if max_wait_seconds is not None:
            self._max_wait_seconds = max_wait_seconds
        self._semaphore = asyncio.Semaphore(concurrency)

    @property
    def max_wait_seconds(self) -> float:
        return self._max_wait_seconds

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def waiting(self) -> int:
        """Calls currently queued for a slot (a queue-depth signal for /metrics)."""
        return self._waiting

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Acquire a slot or, after ``max_wait_seconds``, raise ``ProviderBusyError``: a request must never
        sit in this queue long enough for its own timeout to be what fires."""
        self._waiting += 1
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=self._max_wait_seconds)
            except TimeoutError:
                self.rejected += 1
                log_event("provider.busy", waiting=self._waiting, concurrency=self._concurrency, max_wait_ms=int(self._max_wait_seconds * 1000))
                raise ProviderBusyError("no provider slot within the wait budget") from None
        finally:
            self._waiting -= 1
        try:
            yield
        finally:
            self._semaphore.release()


gate = ProviderGate()

# Module-level so tests can replace the wait without touching asyncio itself.
_sleep = asyncio.sleep


def backoff_delay(attempt: int, retry_after: float | None, *, base: float = 0.5, cap: float = 4.0) -> float:
    """Seconds to wait before ``attempt`` (1-based, the attempt about to be made after a failure).

    The provider's ``Retry-After`` wins when present; otherwise exponential from ``base`` with
    full jitter, capped at ``cap``."""
    if retry_after is not None and retry_after >= 0:
        return float(retry_after)
    exp = min(cap, base * (2 ** max(0, attempt - 2)))
    return random.uniform(0, exp) if exp > 0 else 0.0  # noqa: S311 - jitter, not security


async def call_with_retry(
    call: Callable[[int], Awaitable[T]],
    *,
    max_attempts: int,
    budget_seconds: float,
    stage: str,
    cid: object | None = None,
) -> T:
    """Run ``call(attempt)`` until it succeeds, a non-retryable error is raised, the attempts are
    exhausted, or the next backoff would exceed ``budget_seconds`` of total waiting."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    waited = 0.0
    attempt = 0
    while True:
        attempt += 1
        try:
            return await call(attempt)
        except ProviderError as exc:
            if not exc.retryable or attempt >= max_attempts:
                raise
            delay = backoff_delay(attempt + 1, getattr(exc, "retry_after", None))
            if waited + delay > budget_seconds:
                log_event("provider.retry_exhausted", cid=cid, stage=stage, attempt=attempt, waited_ms=int(waited * 1000), reason=type(exc).__name__)
                raise
            log_event("provider.retry", cid=cid, stage=stage, attempt=attempt, delay_ms=int(delay * 1000), reason=type(exc).__name__)
            await _sleep(delay)
            waited += delay


__all__ = ["ProviderGate", "backoff_delay", "call_with_retry", "gate"]
