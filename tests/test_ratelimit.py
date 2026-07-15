import asyncio

import httpx
from whodap.errors import BadStatusCode, MalformedQueryError, NotFoundError, RateLimitError

from domainscout import ratelimit


def test_token_bucket_zero_rate_never_sleeps():
    waits = []
    async def fake_sleep(d): waits.append(d)
    tb = ratelimit.TokenBucket(0, sleep=fake_sleep, clock=lambda: 0.0)
    async def run():
        await tb.acquire(); await tb.acquire()
    asyncio.run(run())
    assert waits == []


def test_token_bucket_spaces_calls_by_interval():
    waits = []
    async def fake_sleep(d): waits.append(d)
    tb = ratelimit.TokenBucket(2.0, sleep=fake_sleep, clock=lambda: 0.0)  # interval 0.5s
    async def run():
        await tb.acquire()   # first: no wait
        await tb.acquire()   # second: wait one interval
    asyncio.run(run())
    assert waits == [0.5]


def test_with_backoff_retries_then_succeeds():
    calls = []
    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RateLimitError("429")
        return "ok"
    async def fake_sleep(_): pass
    result = asyncio.run(ratelimit.with_backoff(flaky, retries=4, sleep=fake_sleep))
    assert result == "ok" and len(calls) == 3


def test_with_backoff_retries_badstatus_and_transport():
    for exc in (BadStatusCode("500"), httpx.ConnectError("boom")):
        calls = []
        async def flaky(_exc=exc):
            calls.append(1)
            if len(calls) < 2:
                raise _exc
            return "ok"
        async def fake_sleep(_): pass
        assert asyncio.run(ratelimit.with_backoff(flaky, retries=3, sleep=fake_sleep)) == "ok"
        assert len(calls) == 2


def test_with_backoff_gives_up_after_retries():
    async def always_429():
        raise RateLimitError("429")
    async def fake_sleep(_): pass
    try:
        asyncio.run(ratelimit.with_backoff(always_429, retries=2, sleep=fake_sleep))
        assert False, "expected RateLimitError"
    except RateLimitError:
        pass


def test_with_backoff_does_not_retry_notfound_or_malformed():
    for exc in (NotFoundError("404"), MalformedQueryError("400")):
        calls = []
        async def once(_exc=exc):
            calls.append(1)
            raise _exc
        async def fake_sleep(_): pass
        try:
            asyncio.run(ratelimit.with_backoff(once, retries=5, sleep=fake_sleep))
        except (NotFoundError, MalformedQueryError):
            pass
        assert len(calls) == 1  # raised immediately, no retries
