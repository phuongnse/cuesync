"""A loopback-only HTTP server that serves byte sources with Range support.

ffmpeg reads a URL with range requests. This server lets it read a file that is still being
downloaded: a read waits until the source has the bytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
from typing import Protocol

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
HEAD_TIMEOUT_S = 10.0
HEAD_LIMIT = 16 * 1024
CHUNK = 64 * 1024

_REASONS = {
    200: "OK",
    206: "Partial Content",
    404: "Not Found",
    405: "Method Not Allowed",
    416: "Range Not Satisfiable",
    503: "Service Unavailable",
}
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


class ByteSource(Protocol):
    size: int

    async def read(self, offset: int, length: int) -> bytes: ...


class MemorySource:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.size = len(data)

    async def read(self, offset: int, length: int) -> bytes:
        return self.data[offset : offset + length]


class _Unsatisfiable(Exception):
    pass


def parse_range(value: str | None, size: int) -> tuple[int, int] | None:
    """The `(start, end)` to serve, inclusive, or None for the whole body.

    Raises `_Unsatisfiable` when the start is past the end of the file. A malformed header,
    or one with several ranges, asks for the whole body.
    """
    if value is None:
        return None
    match = _RANGE.fullmatch(value.strip())
    if not match or (not match[1] and not match[2]):
        return None
    if not match[1]:
        length = int(match[2])
        if length == 0 or size == 0:
            raise _Unsatisfiable
        return max(0, size - length), size - 1
    start = int(match[1])
    if start >= size:
        raise _Unsatisfiable
    end = min(int(match[2]), size - 1) if match[2] else size - 1
    if end < start:
        return None
    return start, end


class LocalServer:
    """Serves sources at `http://127.0.0.1:{port}/{token}`. Use it as `async with`."""

    def __init__(self) -> None:
        self._sources: dict[str, ByteSource] = {}
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._port = 0

    async def start(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(self._handle, HOST, 0, limit=HEAD_LIMIT)
            self._port = self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()

    async def __aenter__(self) -> LocalServer:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def add(self, source: ByteSource) -> str:
        if self._server is None:
            raise RuntimeError("the server is not started")
        token = secrets.token_urlsafe(16)
        self._sources[token] = source
        return f"http://{HOST}:{self._port}/{token}"

    def remove(self, url: str) -> None:
        self._sources.pop(url.rsplit("/", 1)[-1], None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._respond(reader, writer)
        except (
            ConnectionError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            TimeoutError,
        ):
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _respond(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with asyncio.timeout(HEAD_TIMEOUT_S):
            head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        if len(parts) != 3:
            await self._send(writer, 404)
            return
        method, target = parts[0], parts[1]
        if method not in ("GET", "HEAD"):
            await self._send(writer, 405)
            return
        token = target.split("?", 1)[0].strip("/")
        source = self._sources.get(token)
        if source is None:
            await self._send(writer, 404)
            return
        range_header = None
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "range":
                range_header = value
        size = source.size
        try:
            span = parse_range(range_header, size)
        except _Unsatisfiable:
            await self._send(writer, 416, {"Content-Range": f"bytes */{size}"})
            return
        if span is not None:
            start, end = span
            status = 206
            extra = {"Content-Range": f"bytes {start}-{end}/{size}"}
        else:
            start, end = 0, size - 1
            status = 200
            extra = {}
        length = end - start + 1
        first = b""
        if method == "GET" and length > 0:
            try:
                first = await source.read(start, min(CHUNK, length))
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._send(writer, 503)
                return
            if len(first) != min(CHUNK, length):
                await self._send(writer, 503)
                return
        await self._send(writer, status, extra, length)
        if method != "GET" or length == 0:
            return
        writer.write(first)
        await writer.drain()
        sent = len(first)
        while sent < length:
            try:
                chunk = await source.read(start + sent, min(CHUNK, length - sent))
            except asyncio.CancelledError:
                raise
            except Exception:
                return  # the body is cut short, so close the connection
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()
            sent += len(chunk)

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        headers: dict[str, str] | None = None,
        length: int = 0,
    ) -> None:
        lines = [
            f"HTTP/1.1 {status} {_REASONS[status]}",
            "Content-Type: application/octet-stream",
            f"Content-Length: {length}",
            "Accept-Ranges: bytes",
            "Connection: close",
        ]
        lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        await writer.drain()
