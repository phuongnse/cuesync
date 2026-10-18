"""Open the played file straight from its torrent, as a local HTTP URL for ffmpeg."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from cuesync.debrid.files import TorrentFile, find_file
from cuesync.stream import StreamRef
from cuesync.torrent.engine import (
    Engine,
    EngineTorrent,
    TorrentError,
    TorrentInfo,
    TorrentLimitError,
)
from cuesync.torrent.pieces import READ_AHEAD_BYTES, index_ranges, pieces_for_range
from cuesync.torrent.server import LocalServer

log = logging.getLogger(__name__)

READ_AHEAD_DEADLINE_MS = 1000


@dataclass(frozen=True)
class Limits:
    time_s: float = 240.0  # per open, including the wait for the torrent metadata
    max_bytes: int = 1 << 30  # downloaded
    download_rate: int = 0  # bytes per second, 0 for no cap
    upload_rate: int = 256 * 1024


class _State:
    """What the watchdog and the reads share for one open torrent."""

    def __init__(self, engine: Engine, torrent: EngineTorrent) -> None:
        self.engine = engine
        self.torrent = torrent
        self.failure: TorrentError | None = None
        self._removal: asyncio.Future[None] | None = None

    async def remove(self) -> None:
        """Remove the torrent once. The removal finishes even if the caller is cancelled."""
        if self._removal is None:
            self._removal = asyncio.ensure_future(self.engine.remove(self.torrent))
        await asyncio.shield(self._removal)


class _TorrentSource:
    def __init__(
        self,
        state: _State,
        info: TorrentInfo,
        file: TorrentFile,
        offset: int,
        path: Path,
        poll_s: float,
    ) -> None:
        self.size = file.size
        self._state = state
        self._info = info
        self._offset = offset
        self._path = path
        self._poll_s = poll_s
        self._requested: set[int] = set()

    def _want(self, pieces: range, deadline_ms: int) -> None:
        fresh = [p for p in pieces if p not in self._requested]
        if fresh:
            self._requested.update(fresh)
            self._state.torrent.want(fresh, deadline_ms)

    async def read(self, offset: int, length: int) -> bytes:
        self._check()
        end = min(offset + length, self.size)
        if end <= offset:
            return b""
        length_ = self._info.piece_length
        needed = pieces_for_range(self._offset, length_, offset, end)
        self._want(needed, 0)
        ahead = pieces_for_range(self._offset, length_, end, end + READ_AHEAD_BYTES)
        self._want(
            range(ahead.start, min(ahead.stop, self._info.num_pieces)), READ_AHEAD_DEADLINE_MS
        )
        while not all(self._state.torrent.have(p) for p in needed):
            await asyncio.sleep(self._poll_s)
            self._check()
        return await asyncio.to_thread(self._read_file, offset, end - offset)

    def _check(self) -> None:
        if self._state.failure is not None:
            raise self._state.failure

    def _read_file(self, offset: int, length: int) -> bytes:
        with self._path.open("rb") as f:
            f.seek(offset)
            return f.read(length)


class TorrentReader:
    """Reads one torrent file at a time through a local HTTP URL.

    Only the played file is downloaded: its head and tail first, then the pieces that reads
    reach, with a read-ahead. Leaving `open` removes the torrent and its data.
    """

    def __init__(
        self,
        engine: Engine,
        root: Path,
        limits: Limits = Limits(),  # noqa: B008
        poll_s: float = 0.05,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._root = root
        self._limits = limits
        self._poll_s = poll_s
        self._clock = clock

    @asynccontextmanager
    async def open(self, stream: StreamRef) -> AsyncIterator[str]:
        """Yield a loopback URL of the played file of `stream`."""
        folder = self._root / uuid.uuid4().hex
        deadline = self._clock() + self._limits.time_s
        state: _State | None = None
        watchdog: asyncio.Task[None] | None = None
        server = LocalServer()
        try:
            folder.mkdir(parents=True)
            log.info("reading torrent %s", stream.info_hash)
            torrent = self._engine.add(
                stream.info_hash,
                folder,
                trackers=stream.trackers,
                download_limit=self._limits.download_rate,
                upload_limit=self._limits.upload_rate,
            )
            state = _State(self._engine, torrent)
            info = await self._metadata(torrent, deadline)
            file = find_file(info.files, stream)
            if file is None:
                raise TorrentError("the played file is not in the torrent")
            offset = info.offsets[info.files.index(file)]
            torrent.select(file.index)
            for start, end in index_ranges(file.size):
                torrent.want(pieces_for_range(offset, info.piece_length, start, end), 0)
            watchdog = asyncio.create_task(self._watch(state, deadline))
            await server.start()
            source = _TorrentSource(state, info, file, offset, folder / file.path, self._poll_s)
            yield server.add(source)
        finally:
            await self._cleanup(watchdog, server, state, folder)

    async def _metadata(self, torrent: EngineTorrent, deadline: float) -> TorrentInfo:
        while (info := torrent.info()) is None:
            if self._clock() > deadline:
                raise TorrentLimitError("the torrent metadata did not arrive in time")
            await asyncio.sleep(self._poll_s)
        return info

    async def _watch(self, state: _State, deadline: float) -> None:
        while True:
            await asyncio.sleep(self._poll_s)
            if state.torrent.downloaded() > self._limits.max_bytes:
                state.failure = TorrentLimitError("the download limit was passed")
            elif self._clock() > deadline:
                state.failure = TorrentLimitError("the time limit was passed")
            else:
                continue
            await state.remove()
            return

    async def _cleanup(
        self,
        watchdog: asyncio.Task[None] | None,
        server: LocalServer,
        state: _State | None,
        folder: Path,
    ) -> None:
        if watchdog is not None:
            watchdog.cancel()
            with contextlib.suppress(BaseException):
                await watchdog
        try:
            await server.close()
        except Exception:
            log.warning("closing the local server failed", exc_info=True)
        if state is not None:
            try:
                await state.remove()
            except Exception:
                log.warning("removing the torrent failed", exc_info=True)
        shutil.rmtree(folder, ignore_errors=True)
