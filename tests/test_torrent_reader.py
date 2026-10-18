"""Tests for the torrent reader, on a fake torrent engine that writes real files."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

import httpx
import pytest

from cuesync import audio
from cuesync.debrid.files import TorrentFile
from cuesync.stream import StreamRef
from cuesync.torrent import (
    LibtorrentEngine,
    Limits,
    TorrentError,
    TorrentReader,
    index_ranges,
    reader,
)
from cuesync.torrent.engine import TorrentInfo

HASH = "ab" * 20
PIECE = 16 * 1024
README = b"hello torrent\n" * 700  # 9800 bytes, so the mkv starts in the middle of piece 0
SMALL = 32 * 1024  # a head and tail of two pieces keep a read of a 30 s file partial


class FakeTorrent:
    def __init__(self, engine: FakeEngine, save_path: Path) -> None:
        self.engine = engine
        self.save_path = save_path
        self.blob = engine.blob
        self.selected: int | None = None
        self.pieces: set[int] = set()
        self.wanted: list[tuple[int, int]] = []
        self.bytes_down = 0
        self.added_at = 0

    def info(self) -> TorrentInfo | None:
        self.engine.polls += 1
        if self.engine.polls <= self.engine.metadata_polls:
            return None
        return self.engine.torrent_info

    def select(self, file_index: int) -> None:
        self.selected = file_index

    def want(self, pieces: Iterable[int], deadline_ms: int) -> None:
        info = self.engine.torrent_info
        assert self.selected is not None
        file = info.files[self.selected]
        start = info.offsets[self.selected]
        for piece in pieces:
            self.wanted.append((piece, deadline_ms))
            lo = max(piece * PIECE, start)
            hi = min((piece + 1) * PIECE, start + file.size)
            if hi > lo:
                path = self.save_path / file.path
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("r+b" if path.exists() else "wb") as f:
                    f.seek(lo - start)
                    f.write(self.blob[lo:hi])
            self.pieces.add(piece)
            self.bytes_down += PIECE

    def have(self, piece: int) -> bool:
        return piece in self.pieces

    def downloaded(self) -> int:
        return self.bytes_down + self.engine.extra_bytes


class FakeEngine:
    def __init__(self, mkv: bytes) -> None:
        self.blob = README + mkv
        self.mkv_size = len(mkv)
        self.torrent_info = TorrentInfo(
            piece_length=PIECE,
            num_pieces=-(-len(self.blob) // PIECE),
            files=(
                TorrentFile(0, "Show/readme.txt", len(README)),
                TorrentFile(1, "Show/Movie.2010.mkv", len(mkv)),
            ),
            offsets=(0, len(README)),
        )
        self.metadata_polls = 0
        self.polls = 0
        self.extra_bytes = 0
        self.torrents: list[FakeTorrent] = []
        self.removed: list[FakeTorrent] = []
        self.limits: tuple[int, int] | None = None

    def add(self, info_hash: str, save_path: Path, *, download_limit: int, upload_limit: int):
        assert info_hash == HASH
        assert save_path.is_dir()
        self.limits = (download_limit, upload_limit)
        torrent = FakeTorrent(self, save_path)
        self.torrents.append(torrent)
        return torrent

    async def remove(self, torrent: FakeTorrent) -> None:
        await asyncio.sleep(0)
        self.removed.append(torrent)
        shutil.rmtree(torrent.save_path, ignore_errors=True)
        torrent.save_path.mkdir()  # the reader deletes the folder itself


@pytest.fixture
def engine(mkv_bytes: bytes) -> FakeEngine:
    return FakeEngine(mkv_bytes)


@pytest.fixture
def small_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "index_ranges", lambda size: index_ranges(size, SMALL, SMALL))
    monkeypatch.setattr(reader, "READ_AHEAD_BYTES", SMALL)


def stream(**overrides) -> StreamRef:
    fields = {"info_hash": HASH, "file_idx": 1, "filename": "Movie.2010.mkv", "size": None}
    return StreamRef(**{**fields, **overrides})


def run_open(
    engine: FakeEngine,
    root: Path,
    check: Callable[[str], Awaitable[None]],
    *,
    ref: StreamRef | None = None,
    limits: Limits | None = None,
    poll_s: float = 0.01,
) -> None:
    async def main() -> None:
        async with TorrentReader(engine, root, limits or Limits(), poll_s).open(
            ref or stream()
        ) as url:
            await check(url)

    asyncio.run(main())


def assert_cleaned(engine: FakeEngine, root: Path) -> None:
    assert len(engine.removed) == len(engine.torrents) == 1
    assert list(root.iterdir()) == []


def test_probe_through_open_reads_only_some_pieces(
    engine: FakeEngine, tmp_path: Path, small_index: None
):
    seen: list[float] = []

    async def check(url: str) -> None:
        assert url.startswith("http://127.0.0.1:")
        seen.append((await audio.probe(url)).duration_s)

    run_open(engine, tmp_path, check)
    assert seen == [pytest.approx(30.0, abs=0.1)]
    torrent = engine.torrents[0]
    assert torrent.selected == 1
    assert 0 < len(torrent.pieces) < engine.torrent_info.num_pieces
    # head and tail are asked first, with the earliest deadline
    assert [deadline for _, deadline in torrent.wanted[:4]] == [0, 0, 0, 0]
    assert engine.limits == (0, 256 * 1024)
    assert_cleaned(engine, tmp_path)


def test_bytes_through_the_url_are_the_file(engine: FakeEngine, tmp_path: Path, mkv_bytes: bytes):
    async def check(url: str) -> None:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers={"Range": "bytes=5000-40000"})
            assert r.status_code == 206
            assert r.content == mkv_bytes[5000:40001]
            r = await client.get(url, headers={"Range": "bytes=-100"})
            assert r.content == mkv_bytes[-100:]

    run_open(engine, tmp_path, check)


def test_waits_for_the_metadata(engine: FakeEngine, tmp_path: Path):
    engine.metadata_polls = 5
    run_open(engine, tmp_path, lambda url: asyncio.sleep(0))
    assert engine.polls > 5
    assert_cleaned(engine, tmp_path)


def test_exception_removes_the_torrent(engine: FakeEngine, tmp_path: Path):
    async def check(url: str) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_open(engine, tmp_path, check)
    assert_cleaned(engine, tmp_path)


def test_cancellation_removes_the_torrent(engine: FakeEngine, tmp_path: Path):
    async def main() -> None:
        entered = asyncio.Event()

        async def hold() -> None:
            async with TorrentReader(engine, tmp_path, poll_s=0.01).open(stream()):
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(hold())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert_cleaned(engine, tmp_path)


def test_cancellation_while_waiting_for_metadata(engine: FakeEngine, tmp_path: Path):
    engine.metadata_polls = 10**9

    async def main() -> None:
        async def hold() -> None:
            async with TorrentReader(engine, tmp_path, poll_s=0.01).open(stream()):
                raise AssertionError("opened")

        task = asyncio.create_task(hold())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert_cleaned(engine, tmp_path)


def test_byte_cap_fails_reads_and_removes_the_torrent(engine: FakeEngine, tmp_path: Path):
    async def check(url: str) -> None:
        engine.extra_bytes = 10 * PIECE
        await asyncio.sleep(0.2)
        assert engine.removed  # removed before the context exits
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers={"Range": "bytes=0-99"})
        assert r.status_code == 503

    run_open(engine, tmp_path, check, limits=Limits(max_bytes=PIECE))
    assert_cleaned(engine, tmp_path)


def test_byte_cap_passed_by_the_head_and_tail_fetch(engine: FakeEngine, tmp_path: Path):
    async def check(url: str) -> None:
        await asyncio.sleep(0.2)
        assert engine.removed
        async with httpx.AsyncClient() as client:
            assert (await client.get(url)).status_code == 503

    run_open(engine, tmp_path, check, limits=Limits(max_bytes=1))
    assert_cleaned(engine, tmp_path)


def test_time_limit_fails_reads_and_removes_the_torrent(engine: FakeEngine, tmp_path: Path):
    async def check(url: str) -> None:
        await asyncio.sleep(0.5)
        assert engine.removed
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers={"Range": "bytes=0-99"})
        assert r.status_code == 503

    run_open(engine, tmp_path, check, limits=Limits(time_s=0.2))
    assert_cleaned(engine, tmp_path)


def test_time_limit_covers_the_metadata_wait(engine: FakeEngine, tmp_path: Path):
    engine.metadata_polls = 10**9

    async def check(url: str) -> None:
        raise AssertionError("opened")

    with pytest.raises(TorrentError, match="metadata"):
        run_open(engine, tmp_path, check, limits=Limits(time_s=0.2))
    assert_cleaned(engine, tmp_path)


def test_unmatched_file_raises_and_cleans_up(engine: FakeEngine, tmp_path: Path):
    ref = stream(file_idx=1, filename="Other.mkv", size=12345)

    async def check(url: str) -> None:
        raise AssertionError("opened")

    with pytest.raises(TorrentError, match="not in the torrent"):
        run_open(engine, tmp_path, check, ref=ref)
    assert_cleaned(engine, tmp_path)


def test_file_is_picked_by_the_debrid_rule(engine: FakeEngine, tmp_path: Path, mkv_bytes: bytes):
    async def check(url: str) -> None:
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers={"Range": "bytes=0-9"})
        assert r.content == mkv_bytes[:10]

    run_open(engine, tmp_path, check, ref=stream(file_idx=None, filename=None, size=None))
    assert engine.torrents[0].selected == 1


def test_missing_libtorrent_names_the_extra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setitem(sys.modules, "libtorrent", None)
    with pytest.raises(TorrentError, match=r"cuesync\[torrent\]"):
        LibtorrentEngine().add(HASH, tmp_path, download_limit=0, upload_limit=0)


def test_package_imports_without_importing_libtorrent():
    code = "import sys, cuesync.torrent; assert 'libtorrent' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
