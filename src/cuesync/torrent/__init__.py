"""The torrent reader: serves the played file of a torrent on a loopback HTTP URL for ffmpeg."""

from __future__ import annotations

from cuesync.torrent.engine import LibtorrentEngine, TorrentError, TorrentLimitError
from cuesync.torrent.pieces import index_ranges, pieces_for_range
from cuesync.torrent.reader import Limits, TorrentReader
from cuesync.torrent.server import LocalServer, MemorySource
from cuesync.torrent.settings import TorrentSettings, torrent_settings

__all__ = [
    "LibtorrentEngine",
    "Limits",
    "LocalServer",
    "MemorySource",
    "TorrentError",
    "TorrentLimitError",
    "TorrentReader",
    "TorrentSettings",
    "index_ranges",
    "pieces_for_range",
    "torrent_settings",
]
