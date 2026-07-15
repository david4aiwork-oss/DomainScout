"""Async politeness helpers for RDAP: a token-bucket pacer and an exponential-backoff retry
wrapper. Both take injected sleep/clock so tests run with no real waiting."""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

import httpx
from whodap.errors import BadStatusCode, RateLimitError

# Retry these (429 / 5xx / network+timeout). NotFoundError = available signal (not retried);
# MalformedQueryError = a bad query that won't fix on retry (-> caller's errors bucket).
RETRYABLE = (RateLimitError, BadStatusCode, httpx.TransportError)


class TokenBucket:
    """Ensures >= 1/rate seconds between acquire() releases. rate<=0 disables pacing."""

    def __init__(self, rate: float, *, sleep=asyncio.sleep, clock=time.monotonic) -> None:
        self._interval = (1.0 / rate) if rate and rate > 0 else 0.0
        self._sleep = sleep
        self._clock = clock
        self._next_time: float | None = None

    async def acquire(self) -> None:
        if self._interval <= 0:
            return
        now = self._clock()
        # Reserve this slot SYNCHRONOUSLY (before any await) so concurrent coroutines each get a
        # distinct, properly-spaced slot instead of all reading the same _next_time and racing.
        start = self._next_time if (self._next_time is not None and self._next_time > now) else now
        self._next_time = start + self._interval
        wait = start - now
        if wait > 0:
            await self._sleep(wait)


async def with_backoff(
    coro_factory: Callable[[], Awaitable],
    *,
    retries: int,
    base: float = 2.0,
    cap: float = 60.0,
    sleep=asyncio.sleep,
):
    """Call coro_factory(); on a RETRYABLE error retry with exponential delay, up to `retries`
    extra attempts, then re-raise. Non-RETRYABLE errors propagate immediately."""
    for attempt in range(retries + 1):
        try:
            return await coro_factory()
        except RETRYABLE:
            if attempt >= retries:
                raise
            await sleep(min(cap, base * (2 ** attempt)))
