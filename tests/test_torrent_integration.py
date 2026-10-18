"""Opt-in test of the torrent fallback against a local libtorrent seeder.

Run it with `CUESYNC_TORRENT_INTEGRATION=1`. It needs the `torrent` extra. It is skipped on CI.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from pathlib import Path

import pytest

from cuesync import audio
from cuesync.stream import StreamRef
from cuesync.torrent import LibtorrentEngine, Limits, TorrentReader

pytestmark = pytest.mark.skipif(
    os.environ.get("CUESYNC_TORRENT_INTEGRATION") != "1",
    reason="set CUESYNC_TORRENT_INTEGRATION=1 to run the libtorrent test",
)

PIECE_LENGTH = 16 * 1024


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_audio_windows_are_read_from_a_local_seeder(tmp_path: Path, mkv_bytes: bytes):
    lt = pytest.importorskip("libtorrent")

    content = tmp_path / "content"
    content.mkdir()
    (content / "Movie.2010.mkv").write_bytes(mkv_bytes)
    storage = lt.file_storage()
    lt.add_files(storage, str(content / "Movie.2010.mkv"))
    creator = lt.create_torrent(storage, PIECE_LENGTH)
    lt.set_piece_hashes(creator, str(content))
    metadata = lt.bdecode(lt.bencode(creator.generate()))
    info = lt.torrent_info(metadata)

    port = free_port()
    seeder = lt.session(
        {
            "listen_interfaces": f"127.0.0.1:{port}",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "allow_multiple_connections_per_ip": True,
        }
    )
    seeded = seeder.add_torrent({"ti": info, "save_path": str(content)})
    deadline = time.monotonic() + 30
    while not seeded.status().is_seeding and time.monotonic() < deadline:
        time.sleep(0.1)
    assert seeded.status().is_seeding

    class LocalEngine(LibtorrentEngine):
        def add(self, info_hash, save_path, **kwargs):
            torrent = super().add(info_hash, save_path, **kwargs)
            torrent.handle.connect_peer(("127.0.0.1", port))
            return torrent

    engine = LocalEngine()
    root = tmp_path / "torrents"
    reader = TorrentReader(engine, root, Limits(time_s=60))
    stream = StreamRef(str(info.info_hash()), 0, "Movie.2010.mkv", len(mkv_bytes))

    async def main() -> list[audio.AudioWindow]:
        async with reader.open(stream) as url:
            media = await audio.probe(url, timeout=60)
            return await audio.extract_windows(
                url,
                media.duration_s,
                count=2,
                length_s=10,
                edge_s=0,
                max_concurrent=1,
                timeout_s=60,
            )

    try:
        windows = asyncio.run(main())
        assert len(windows) == 2
        assert all(window.samples for window in windows)
        assert not root.exists() or list(root.iterdir()) == []
    finally:
        engine.close()
        seeder.pause()
