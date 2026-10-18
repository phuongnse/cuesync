"""What the torrent reader needs from a torrent engine, and the libtorrent adapter."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cuesync.debrid.files import TorrentFile

log = logging.getLogger(__name__)

TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
)
REMOVE_WAIT_S = 5.0
REMOVE_POLL_S = 0.05
HIGHEST_PRIORITY = 7


class TorrentError(Exception):
    """The torrent could not be read. Messages never hold a URL or a token."""


class TorrentLimitError(TorrentError):
    """The time or byte limit of a read was passed."""


@dataclass(frozen=True)
class TorrentInfo:
    piece_length: int
    num_pieces: int
    files: tuple[TorrentFile, ...]
    offsets: tuple[int, ...]  # byte offset in the torrent of each file, same order as `files`


class EngineTorrent(Protocol):
    def info(self) -> TorrentInfo | None:
        """None until the metadata has arrived."""
        ...

    def select(self, file_index: int) -> None:
        """Download only this file. No piece is wanted until `want` asks for it."""
        ...

    def want(self, pieces: Iterable[int], deadline_ms: int) -> None: ...

    def have(self, piece: int) -> bool: ...

    def downloaded(self) -> int:
        """Payload bytes downloaded since the torrent was added."""
        ...


class Engine(Protocol):
    def add(
        self,
        info_hash: str,
        save_path: Path,
        *,
        trackers: Sequence[str],
        download_limit: int,
        upload_limit: int,
    ) -> EngineTorrent:
        """Start the torrent. `trackers` come on top of the built-in ones."""
        ...

    async def remove(self, torrent: EngineTorrent) -> None:
        """Stop the torrent and delete its files."""
        ...

    def close(self) -> None:
        """Stop the session. The engine is not used afterwards."""
        ...


def _lt() -> Any:
    try:
        import libtorrent
    except ImportError:
        raise TorrentError("libtorrent is not installed: pip install 'cuesync[torrent]'") from None
    return libtorrent


class LibtorrentTorrent:
    def __init__(self, handle: Any) -> None:
        self.handle = handle

    def info(self) -> TorrentInfo | None:
        if not self.handle.is_valid() or not self.handle.status().has_metadata:
            return None
        torrent = self.handle.torrent_file()
        files = torrent.files()
        count = files.num_files()
        return TorrentInfo(
            piece_length=torrent.piece_length(),
            num_pieces=torrent.num_pieces(),
            files=tuple(
                TorrentFile(i, files.file_path(i), files.file_size(i)) for i in range(count)
            ),
            offsets=tuple(files.file_offset(i) for i in range(count)),
        )

    def select(self, file_index: int) -> None:
        info = self.info()
        if info is None:
            raise TorrentError("the torrent metadata is not available")
        self.handle.prioritize_files([int(i == file_index) for i in range(len(info.files))])
        self.handle.prioritize_pieces([0] * info.num_pieces)

    def want(self, pieces: Iterable[int], deadline_ms: int) -> None:
        for piece in pieces:
            self.handle.piece_priority(piece, HIGHEST_PRIORITY)
            self.handle.set_piece_deadline(piece, deadline_ms)

    def have(self, piece: int) -> bool:
        return bool(self.handle.have_piece(piece))

    def downloaded(self) -> int:
        return int(self.handle.status().total_payload_download)


class LibtorrentEngine:
    """Downloads with libtorrent, which comes from the optional `torrent` extra."""

    def __init__(self) -> None:
        self._session: Any = None

    def _get_session(self) -> Any:
        if self._session is None:
            lt = _lt()
            self._session = lt.session(
                {
                    "listen_interfaces": "0.0.0.0:0,[::]:0",
                    "enable_dht": True,
                    "enable_lsd": False,
                    "enable_upnp": False,
                    "enable_natpmp": False,
                }
            )
        return self._session

    def add(
        self,
        info_hash: str,
        save_path: Path,
        *,
        trackers: Sequence[str],
        download_limit: int,
        upload_limit: int,
    ) -> EngineTorrent:
        lt = _lt()
        session = self._get_session()
        params = lt.parse_magnet_uri(f"magnet:?xt=urn:btih:{info_hash}")
        # Set on the parameters, not in the magnet, so a tracker URL needs no escaping.
        params.trackers = list(dict.fromkeys([*trackers, *TRACKERS]))
        params.save_path = str(save_path)
        params.flags |= lt.torrent_flags.default_dont_download
        params.flags &= ~lt.torrent_flags.auto_managed
        params.flags &= ~lt.torrent_flags.paused
        handle = session.add_torrent(params)
        handle.set_download_limit(download_limit)
        handle.set_upload_limit(upload_limit)
        return LibtorrentTorrent(handle)

    async def remove(self, torrent: EngineTorrent) -> None:
        if not isinstance(torrent, LibtorrentTorrent):
            raise TorrentError("not a libtorrent torrent")
        lt = _lt()
        handle = torrent.handle
        self._get_session().remove_torrent(handle, lt.options_t.delete_files)
        deadline = time.monotonic() + REMOVE_WAIT_S
        while handle.is_valid() and time.monotonic() < deadline:
            await asyncio.sleep(REMOVE_POLL_S)
        if handle.is_valid():
            log.warning("torrent was not removed in time")

    def close(self) -> None:
        if self._session is not None:
            self._session.pause()
            self._session = None
