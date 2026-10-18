"""Tests for the loopback server that serves byte sources with Range support."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import httpx
import pytest

from cuesync import audio
from cuesync.torrent import LocalServer, MemorySource

DATA = bytes(range(256)) * 400  # 102400 bytes, so a full read spans two 64 KiB chunks


def serve(check: Callable[[httpx.AsyncClient, str], Awaitable[None]], data: bytes = DATA) -> None:
    async def main() -> None:
        async with LocalServer() as server, httpx.AsyncClient() as client:
            await check(client, server.add(MemorySource(data)))

    asyncio.run(main())


def test_range_returns_206_with_exact_bytes():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        r = await client.get(url, headers={"Range": "bytes=100-70000"})
        assert r.status_code == 206
        assert r.headers["Content-Range"] == f"bytes 100-70000/{len(DATA)}"
        assert r.headers["Accept-Ranges"] == "bytes"
        assert r.content == DATA[100:70001]

    serve(check)


def test_open_ended_range():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        r = await client.get(url, headers={"Range": "bytes=100000-"})
        assert r.status_code == 206
        assert r.headers["Content-Range"] == f"bytes 100000-{len(DATA) - 1}/{len(DATA)}"
        assert r.content == DATA[100000:]

    serve(check)


def test_suffix_range():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        r = await client.get(url, headers={"Range": "bytes=-500"})
        assert r.status_code == 206
        assert r.content == DATA[-500:]

    serve(check)


def test_end_past_the_file_is_clamped():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        r = await client.get(url, headers={"Range": "bytes=102000-999999"})
        assert r.status_code == 206
        assert r.headers["Content-Range"] == f"bytes 102000-{len(DATA) - 1}/{len(DATA)}"
        assert r.content == DATA[102000:]

    serve(check)


def test_start_past_the_file_is_416():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        r = await client.get(url, headers={"Range": f"bytes={len(DATA)}-"})
        assert r.status_code == 416
        assert r.headers["Content-Range"] == f"bytes */{len(DATA)}"

    serve(check)


def test_without_range_the_whole_file_comes_back():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        r = await client.get(url)
        assert r.status_code == 200
        assert r.content == DATA

    serve(check)


def test_malformed_and_multiple_ranges_get_the_whole_file():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        for value in ("bytes=abc", "bytes=0-1,5-9", "items=0-5"):
            r = await client.get(url, headers={"Range": value})
            assert r.status_code == 200
            assert r.content == DATA

    serve(check)


def test_head_has_headers_and_no_body():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        r = await client.head(url)
        assert r.status_code == 200
        assert r.headers["Content-Length"] == str(len(DATA))
        assert r.content == b""
        r = await client.head(url, headers={"Range": "bytes=10-19"})
        assert r.status_code == 206
        assert r.headers["Content-Length"] == "10"
        assert r.content == b""

    serve(check)


def test_unknown_token_is_404():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        base = url.rsplit("/", 1)[0]
        assert (await client.get(f"{base}/nope")).status_code == 404
        assert (await client.get(f"{base}/")).status_code == 404
        assert (await client.get(f"{url}?x=1")).status_code == 200

    serve(check)


def test_other_methods_are_405():
    async def check(client: httpx.AsyncClient, url: str) -> None:
        assert (await client.post(url)).status_code == 405

    serve(check)


def test_removed_source_is_404():
    async def main() -> None:
        async with LocalServer() as server, httpx.AsyncClient() as client:
            url = server.add(MemorySource(DATA))
            server.remove(url)
            assert (await client.get(url)).status_code == 404

    asyncio.run(main())


def test_urls_are_loopback_and_unique():
    async def main() -> None:
        async with LocalServer() as server:
            first, second = server.add(MemorySource(b"a")), server.add(MemorySource(b"a"))
            assert first.startswith("http://127.0.0.1:")
            assert first != second

    asyncio.run(main())


class FailingSource:
    size = 100

    async def read(self, offset: int, length: int) -> bytes:
        raise OSError("gone")


class StuckSource:
    size = 100

    async def read(self, offset: int, length: int) -> bytes:
        await asyncio.Event().wait()
        return b""


def test_failing_read_is_503():
    async def main() -> None:
        async with LocalServer() as server, httpx.AsyncClient() as client:
            r = await client.get(server.add(FailingSource()))
            assert r.status_code == 503
            assert r.content == b""

    asyncio.run(main())


def test_close_does_not_wait_for_a_stuck_read():
    async def main() -> None:
        async with LocalServer() as server, httpx.AsyncClient() as client:
            request = asyncio.create_task(client.get(server.add(StuckSource())))
            await asyncio.sleep(0.2)
            await asyncio.wait_for(server.close(), 5)
            with contextlib.suppress(httpx.HTTPError):
                await asyncio.wait_for(request, 5)

    asyncio.run(main())


def test_ffprobe_reads_a_mkv_through_the_server(mkv_bytes: bytes):
    async def main() -> float:
        async with LocalServer() as server:
            info = await audio.probe(server.add(MemorySource(mkv_bytes)))
            return info.duration_s

    assert asyncio.run(main()) == pytest.approx(30.0, abs=0.1)
