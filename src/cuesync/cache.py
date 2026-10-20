"""A SQLite and files cache of searches, subtitles, speech timelines and sync results.

Small facts (search results, sync results and the file index) live in SQLite. Subtitles and
speech timelines are files under the data directory, and the least recently used ones are
deleted when they pass the size limit. Sync results of an older sync engine are dropped when the
database is opened, and speech timelines are kept. A cache failure never breaks a request: it is
logged and counts as a miss.
"""

from __future__ import annotations

import functools
import hashlib
import io
import json
import logging
import os
import sqlite3
import threading
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Concatenate

import numpy as np

from cuesync.sources.base import Candidate, SubtitleRequest
from cuesync.subtitles import Cue, parse_text, write_srt
from cuesync.sync import ENGINE_VERSION, Correction, Verdict

SEARCH_TTL_S = 3600
FAILURE_RETRY_S = 24 * 3600
DEFAULT_MAX_BYTES = 500 * 1024 * 1024

# What a broken database, disk or stored file can raise. SubtitleError is a ValueError.
_ERRORS = (sqlite3.Error, OSError, ValueError, TypeError, KeyError, EOFError, zipfile.BadZipFile)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS search (
    key TEXT PRIMARY KEY, candidates TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sync (
    video_hash TEXT, subtitle_key TEXT, verdict TEXT, scale REAL, offset_ms INTEGER,
    agreeing INTEGER, residual_ms REAL, retry_after REAL,
    PRIMARY KEY (video_hash, subtitle_key));
CREATE TABLE IF NOT EXISTS files (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL, used INTEGER NOT NULL);
"""

log = logging.getLogger(__name__)

Timeline = list[tuple[float, np.ndarray]]


def _guarded[**P, R](
    method: Callable[Concatenate[Cache, P], R],
) -> Callable[Concatenate[Cache, P], R | None]:
    """Log a cache failure by type only and return None, so getters miss and setters skip."""

    @functools.wraps(method)
    def wrapper(self: Cache, *args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return method(self, *args, **kwargs)
        except _ERRORS as exc:
            log.warning("cache %s failed: %s", method.__name__, type(exc).__name__)
            return None

    return wrapper


class Cache:
    """The cache. It touches the disk on first use, so building one is free."""

    def __init__(
        self,
        data_dir: Path,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._data_dir = data_dir
        self._max_bytes = max_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def _connect(self) -> sqlite3.Connection:
        """The connection, opened on first use. The caller holds the lock."""
        if self._db is None:
            (self._data_dir / "files").mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(
                self._data_dir / "cuesync.sqlite3", check_same_thread=False, isolation_level=None
            )
            db.executescript(_SCHEMA)
            if db.execute("PRAGMA user_version").fetchone()[0] != ENGINE_VERSION:
                db.execute("DELETE FROM sync")
                db.execute(f"PRAGMA user_version = {int(ENGINE_VERSION)}")
            self._db = db
        return self._db

    # Files. The caller holds the lock.

    def _put_file(self, key: str, suffix: str, data: bytes) -> None:
        db = self._connect()
        # Source ids are external input, so the file name is a hash of the key.
        name = hashlib.sha256(key.encode("utf-8")).hexdigest() + suffix
        path = self._data_dir / "files" / name
        temp = path.with_name(name + ".tmp")
        try:
            temp.write_bytes(data)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        # `used` counts uses instead of telling time, so the eviction order is exact.
        db.execute(
            "INSERT INTO files (key, name, size, used) VALUES "
            "(?, ?, ?, (SELECT COALESCE(MAX(used), 0) + 1 FROM files)) "
            "ON CONFLICT (key) DO UPDATE SET name = excluded.name, size = excluded.size, "
            "used = excluded.used",
            (key, name, len(data)),
        )
        self._evict(db)

    def _evict(self, db: sqlite3.Connection) -> None:
        while True:
            (total,) = db.execute("SELECT COALESCE(SUM(size), 0) FROM files").fetchone()
            if total <= self._max_bytes:
                return
            row = db.execute("SELECT key, name FROM files ORDER BY used LIMIT 1").fetchone()
            if row is None:
                return
            db.execute("DELETE FROM files WHERE key = ?", (row[0],))
            (self._data_dir / "files" / row[1]).unlink(missing_ok=True)

    def _get_file(self, key: str) -> bytes | None:
        db = self._connect()
        row = db.execute("SELECT name FROM files WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            data = (self._data_dir / "files" / row[0]).read_bytes()
        except FileNotFoundError:
            db.execute("DELETE FROM files WHERE key = ?", (key,))
            return None
        db.execute(
            "UPDATE files SET used = (SELECT MAX(used) + 1 FROM files) WHERE key = ?", (key,)
        )
        return data

    # Search results.

    @staticmethod
    def _search_key(source: str, request: SubtitleRequest) -> str:
        # The request holds no API keys, so none can end up in the key.
        raw = json.dumps(
            [
                source,
                request.imdb_id,
                request.season,
                request.episode,
                list(request.languages),
                request.video_hash,
                request.video_size,
                request.filename,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @_guarded
    def get_search(self, source: str, request: SubtitleRequest) -> list[Candidate] | None:
        key = self._search_key(source, request)
        with self._lock:
            db = self._connect()
            row = db.execute(
                "SELECT candidates, expires_at FROM search WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row[1] <= self._clock():
                db.execute("DELETE FROM search WHERE key = ?", (key,))
                return None
        return [Candidate(**item) for item in json.loads(row[0])]

    @_guarded
    def put_search(
        self, source: str, request: SubtitleRequest, candidates: Sequence[Candidate]
    ) -> None:
        # Sources answer with an empty list on errors, so an empty list would hide a source.
        if not candidates:
            return
        key = self._search_key(source, request)
        data = json.dumps([asdict(candidate) for candidate in candidates])
        with self._lock:
            self._connect().execute(
                "INSERT OR REPLACE INTO search (key, candidates, expires_at) VALUES (?, ?, ?)",
                (key, data, self._clock() + SEARCH_TTL_S),
            )

    # Subtitles.

    @_guarded
    def get_subtitle(self, key: str) -> tuple[Cue, ...] | None:
        with self._lock:
            data = self._get_file("sub:" + key)
        if data is None:
            return None
        return tuple(parse_text(data.decode("utf-8")))

    @_guarded
    def put_subtitle(self, key: str, cues: Sequence[Cue]) -> None:
        with self._lock:
            self._put_file("sub:" + key, ".srt", write_srt(cues))

    # Speech timelines.

    @_guarded
    def get_timeline(self, video_hash: str, video_size: int) -> Timeline | None:
        with self._lock:
            data = self._get_file(f"timeline:{video_hash}:{video_size}")
        if data is None:
            return None
        with np.load(io.BytesIO(data), allow_pickle=False) as npz:
            return [(float(start), npz[f"w{i}"]) for i, start in enumerate(npz["starts"])]

    @_guarded
    def put_timeline(self, video_hash: str, video_size: int, windows: Timeline) -> None:
        # An empty timeline would stand in for the audio and block a later real read.
        if not windows:
            return
        buffer = io.BytesIO()
        np.savez(
            buffer,
            starts=np.array([start for start, _ in windows], dtype=np.float64),
            **{f"w{i}": timeline for i, (_, timeline) in enumerate(windows)},
        )
        with self._lock:
            self._put_file(f"timeline:{video_hash}:{video_size}", ".npz", buffer.getvalue())

    # Sync results.

    @_guarded
    def get_sync(self, video_hash: str, subtitle_key: str) -> Correction | None:
        with self._lock:
            db = self._connect()
            row = db.execute(
                "SELECT verdict, scale, offset_ms, agreeing, residual_ms, retry_after "
                "FROM sync WHERE video_hash = ? AND subtitle_key = ?",
                (video_hash, subtitle_key),
            ).fetchone()
            if row is None:
                return None
            if row[5] is not None and row[5] <= self._clock():
                db.execute(
                    "DELETE FROM sync WHERE video_hash = ? AND subtitle_key = ?",
                    (video_hash, subtitle_key),
                )
                return None
        return Correction(Verdict(row[0]), row[1], row[2], row[3], row[4])

    @_guarded
    def put_sync(self, video_hash: str, subtitle_key: str, correction: Correction) -> None:
        failed = correction.verdict is Verdict.FAILED
        with self._lock:
            self._connect().execute(
                "INSERT OR REPLACE INTO sync (video_hash, subtitle_key, verdict, scale, "
                "offset_ms, agreeing, residual_ms, retry_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    video_hash,
                    subtitle_key,
                    correction.verdict.value,
                    correction.scale,
                    correction.offset_ms,
                    correction.agreeing,
                    correction.residual_ms,
                    self._clock() + FAILURE_RETRY_S if failed else None,
                ),
            )
