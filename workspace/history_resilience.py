"""Circuit breaker and backoff helpers for the run-history store.

The history store talks to a disk that may fill up or be unmounted (the
project lives on an external volume). These helpers keep that failure from
cascading: repeated errors open a circuit, exactly one probe is admitted when
it half-opens, and retries use exponential backoff with jitter.

Key classes / functions:
    CircuitBreaker: async breaker with a single-probe half-open latch.
    CircuitOpenError: raised when a call is rejected without being attempted.
    backoff_delay: ``min(base * 2**attempt, cap) + jitter`` in seconds.

Usage:
    breaker = CircuitBreaker(failure_threshold=3, reset_timeout=2.0)
    result = await breaker.call(operation, is_failure=lambda e: isinstance(e, OSError))

Dependencies: Python 3.11+ standard library and ``app.exceptions``.
Tier 2: breaker misuse is loud (CircuitOpenError); it never hides data loss itself.
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TypeVar

from app.exceptions import OpenManusError

T = TypeVar("T")


class CircuitOpenError(OpenManusError):
    """Raised when the breaker rejects a call without attempting it."""


class BreakerState(str, Enum):
    """Breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


def backoff_delay(
    attempt: int,
    base: float,
    cap: float,
    jitter: Callable[[float, float], float] = random.uniform,
) -> float:
    """Return the delay before retry ``attempt`` (0-based) in seconds.

    Args:
        attempt: Zero-based retry number.
        base: Base delay in seconds.
        cap: Maximum exponential component in seconds.
        jitter: ``(low, high) -> float`` source; injectable for tests.

    Returns:
        ``min(base * 2**attempt, cap)`` plus jitter in ``[0, base]``.

    Example:
        >>> backoff_delay(2, 0.1, 5.0, jitter=lambda lo, hi: 0.0)
        0.4
    """
    return min(base * (2**attempt), cap) + jitter(0.0, base)


class CircuitBreaker:
    """Async circuit breaker whose half-open state admits exactly one probe.

    Args:
        failure_threshold: Consecutive failures that open the circuit.
        reset_timeout: Seconds the circuit stays open before a probe is allowed.
        clock: Monotonic time source; injectable for tests.

    Example:
        >>> breaker = CircuitBreaker(2, 1.0)
        >>> breaker.state.value
        'closed'

    Note:
        Transitions are serialized by an ``asyncio.Lock``. Elapsed time uses a
        monotonic clock, never wall time.
    """

    def __init__(
        self,
        failure_threshold: int,
        reset_timeout: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._clock = clock
        self._lock = asyncio.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        """Current breaker state."""
        return self._state

    def remaining_open(self) -> float:
        """Seconds until the next probe is allowed (0 when not open)."""
        if self._state is not BreakerState.OPEN:
            return 0.0
        return max(0.0, self._reset_timeout - (self._clock() - self._opened_at))

    async def _admit(self) -> None:
        """Admit the caller or raise CircuitOpenError."""
        async with self._lock:
            if self._state is BreakerState.CLOSED:
                return
            if self._state is BreakerState.OPEN:
                if self._clock() - self._opened_at < self._reset_timeout:
                    raise CircuitOpenError("circuit open")
                self._state = BreakerState.HALF_OPEN
            if self._probe_in_flight:
                raise CircuitOpenError("probe in flight")
            self._probe_in_flight = True

    async def _succeed(self) -> None:
        async with self._lock:
            self._state = BreakerState.CLOSED
            self._failures = 0
            self._probe_in_flight = False

    async def _fail(self) -> None:
        async with self._lock:
            self._failures += 1
            if (
                self._state is BreakerState.HALF_OPEN
                or self._failures >= self._threshold
            ):
                self._state = BreakerState.OPEN
                self._opened_at = self._clock()
            self._probe_in_flight = False

    async def _neutral(self) -> None:
        async with self._lock:
            self._probe_in_flight = False

    async def call(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        is_failure: Callable[[BaseException], bool] = lambda exc: True,
    ) -> T:
        """Run ``operation`` through the breaker.

        Args:
            operation: Zero-argument coroutine function to execute.
            is_failure: Decides whether a raised exception counts against the
                circuit. Exceptions that are not failures (e.g. a logical
                validation error) leave the circuit healthy.

        Returns:
            The operation's result.

        Raises:
            CircuitOpenError: The circuit is open or a probe is already running.
            BaseException: Whatever ``operation`` raised, after bookkeeping.

        Example:
            >>> await CircuitBreaker(3, 1.0).call(lambda: asyncio.sleep(0, "ok"))
            'ok'
        """
        await self._admit()
        try:
            result = await operation()
        except asyncio.CancelledError:
            await self._neutral()
            raise
        except BaseException as exc:
            if is_failure(exc):
                await self._fail()
            else:
                await self._succeed()
            raise
        await self._succeed()
        return result


__all__ = ["BreakerState", "CircuitBreaker", "CircuitOpenError", "backoff_delay"]
