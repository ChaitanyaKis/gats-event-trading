from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import respx

from gats.net import FetchError, PoliteClient
from tests.conftest import SleepRecorder

URL = "https://example.test/data"
HOME = "https://example.test/"


def client(sleeper: SleepRecorder, *, retries: int = 2, interval: float = 0) -> PoliteClient:
    return PoliteClient(
        user_agent="test",
        min_interval_s=interval,
        max_retries=retries,
        backoff_base_s=0.5,
        backoff_max_s=30,
        sleep=sleeper,
    )


@respx.mock
async def test_retries_then_succeeds(sleeper: SleepRecorder) -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, text="ok")])
    async with client(sleeper) as c:
        got = await c.get(URL)
    assert got.ok and got.content == b"ok"
    assert route.call_count == 2
    assert len(sleeper.calls) == 1


@respx.mock
async def test_honours_retry_after(sleeper: SleepRecorder) -> None:
    respx.get(URL).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    async with client(sleeper) as c:
        await c.get(URL)
    assert sleeper.calls == [7.0]


@respx.mock
async def test_not_found_is_returned_not_raised(sleeper: SleepRecorder) -> None:
    respx.get(URL).mock(return_value=httpx.Response(404))
    async with client(sleeper) as c:
        got = await c.get(URL)
    assert got.status == 404 and not got.ok
    assert sleeper.calls == []


@respx.mock
async def test_persistent_failure_raises(sleeper: SleepRecorder) -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(502))
    async with client(sleeper, retries=2) as c:
        with pytest.raises(FetchError) as info:
            await c.get(URL)
    assert info.value.status == 502
    assert route.call_count == 3


@respx.mock
async def test_transport_errors_are_retried(sleeper: SleepRecorder) -> None:
    respx.get(URL).mock(side_effect=[httpx.ConnectError("down"), httpx.Response(200)])
    async with client(sleeper) as c:
        got = await c.get(URL)
    assert got.ok


@respx.mock
async def test_forbidden_triggers_rewarm(sleeper: SleepRecorder) -> None:
    home = respx.get(HOME).mock(return_value=httpx.Response(200, text="<html>"))
    respx.get(URL).mock(side_effect=[httpx.Response(403), httpx.Response(200, text="{}")])
    async with client(sleeper) as c:
        got = await c.get(URL, warmup_url=HOME)
    assert got.ok
    assert home.call_count == 2  # initial warm-up + forced re-warm


@respx.mock
async def test_warmup_is_cached(sleeper: SleepRecorder) -> None:
    home = respx.get(HOME).mock(return_value=httpx.Response(200))
    respx.get(URL).mock(return_value=httpx.Response(200))
    async with client(sleeper) as c:
        await c.get(URL, warmup_url=HOME)
        await c.get(URL, warmup_url=HOME)
    assert home.call_count == 1


@respx.mock
async def test_per_host_throttle(sleeper: SleepRecorder) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200))
    async with client(sleeper, interval=5.0) as c:
        await c.get(URL)
        await c.get(URL)
    assert len(sleeper.calls) == 1
    assert 4.5 < sleeper.calls[0] <= 5.0


@respx.mock
async def test_max_bytes_uses_declared_length(sleeper: SleepRecorder) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=b"x" * 2000))
    async with client(sleeper) as c:
        got = await c.get(URL, max_bytes=1000)
        small = await c.get(URL, max_bytes=5000)
    assert got.too_large and got.content == b"" and not got.ok
    assert small.ok and len(small.content) == 2000


@respx.mock
async def test_max_bytes_stops_streaming_without_length(sleeper: SleepRecorder) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"y" * 500

    respx.get(URL).mock(return_value=httpx.Response(200, content=chunks()))
    async with client(sleeper) as c:
        got = await c.get(URL, max_bytes=1200)
    assert got.too_large


@respx.mock
async def test_per_host_interval_override(sleeper: SleepRecorder) -> None:
    other = "https://slow.test/x"
    respx.get(URL).mock(return_value=httpx.Response(200))
    respx.get(other).mock(return_value=httpx.Response(200))
    c = PoliteClient(
        user_agent="t",
        min_interval_s=0,
        host_min_interval_s={"slow.test": 3.0},
        max_retries=0,
        sleep=sleeper,
    )
    async with c:
        await c.get(URL)
        await c.get(URL)
        await c.get(other)
        await c.get(other)
    assert len(sleeper.calls) == 1 and 2.5 < sleeper.calls[0] <= 3.0
