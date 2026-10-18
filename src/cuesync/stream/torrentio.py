"""Torrentio stream lookup: find the torrent and the file inside it that Stremio is playing."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import httpx

from cuesync import __version__
from cuesync.stremio import VideoId

DEFAULT_URL = "https://torrentio.strem.fun"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
CACHE_TTL = 5 * 60  # seconds
INFO_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# A Torrentio title is the torrent name, then optionally the file name, then a line of
# seeders, size and indexer, then optionally a line of language flags.
_STATS_MARKS = ("👤", "💾", "⚙️")

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and a Torrentio config path can hold a debrid key.
logging.getLogger("httpx").setLevel(logging.WARNING)


@dataclass(frozen=True)
class StreamRef:
    """The torrent and the file inside it that match the played file."""

    info_hash: str  # lowercase hex
    file_idx: int | None  # None when Torrentio omits it, for single-file torrents
    filename: str | None
    size: int | None
    trackers: tuple[str, ...] = ()  # tracker URLs from Torrentio, to find peers faster


@dataclass(frozen=True)
class _Stream:
    info_hash: str
    file_idx: int | None
    filename: str | None
    size: int | None
    trackers: tuple[str, ...] = ()


# request URL -> (expires_at on the monotonic clock, parsed streams)
_cache: dict[str, tuple[float, list[_Stream]]] = {}


def clear_cache() -> None:
    _cache.clear()


async def find_stream(
    client: httpx.AsyncClient,
    video: VideoId,
    filename: str | None,
    size: int | None,
    *,
    base_url: str | None = None,
) -> StreamRef | None:
    """Return the one stream that matches the played file, or None without a confident match.

    A file name match wins, and a known size that differs rules a stream out. Without a name,
    or when no stream has that name, an exact size match is used. Two different torrents that
    match equally are ambiguous. Failures are logged without the base URL and give None.
    """
    if not filename and not size:
        return None
    base = _base_url(base_url)
    if base is None:
        return None
    kind = "series" if video.season is not None else "movie"
    video_id = video.imdb_id
    if video.season is not None:
        video_id += f":{video.season}:{video.episode}"
    url = f"{base}/stream/{kind}/{video_id}.json"

    streams = await _streams(client, url)
    if streams is None:
        return None
    match = _match(streams, filename or None, size if size and size > 0 else None)
    if match is None:
        return None
    return StreamRef(
        info_hash=match.info_hash,
        file_idx=match.file_idx,
        filename=match.filename,
        size=match.size or (size if size and size > 0 else None),
        trackers=match.trackers,
    )


def _base_url(base_url: str | None) -> str | None:
    raw = base_url if base_url is not None else os.environ.get("CUESYNC_TORRENTIO_URL")
    value = (raw or "").strip() or DEFAULT_URL
    if not value.startswith(("http://", "https://")):
        log.warning("Torrentio lookup skipped: the base URL must start with http:// or https://")
        return None
    return value.rstrip("/")


async def _streams(client: httpx.AsyncClient, url: str) -> list[_Stream] | None:
    now = time.monotonic()
    cached = _cache.get(url)
    if cached is not None and cached[0] > now:
        return cached[1]

    try:
        response = await client.get(
            url,
            headers={"User-Agent": f"CueSync v{__version__}", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except httpx.TimeoutException:
        return _fail("Torrentio request timed out")
    except httpx.HTTPError as exc:
        return _fail(f"Torrentio request failed ({type(exc).__name__})")
    if response.is_error:
        return _fail(f"Torrentio returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError:
        return _fail("Torrentio sent an answer that is not JSON")
    items = body.get("streams") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return _fail("Torrentio sent an unexpected answer")

    streams = _parse(items)
    for key in [key for key, (expires, _) in _cache.items() if expires <= now]:
        del _cache[key]
    _cache[url] = (now + CACHE_TTL, streams)
    return streams


def _fail(reason: str) -> None:
    # The reason never holds the URL, because a Torrentio config path can hold a debrid key.
    log.warning("Torrentio lookup failed: %s", reason)
    return None


def _parse(items: list[Any]) -> list[_Stream]:
    streams: list[_Stream] = []
    for item in items:
        try:
            info_hash = item["infoHash"]
            if not isinstance(info_hash, str) or not INFO_HASH_RE.match(info_hash):
                continue
            file_idx = item.get("fileIdx")
            if not isinstance(file_idx, int) or isinstance(file_idx, bool):
                file_idx = None
            hints = item.get("behaviorHints")
            hints = hints if isinstance(hints, dict) else {}
            filename = hints.get("filename")
            if not isinstance(filename, str) or not filename.strip():
                filename = _title_filename(item.get("title"))
            size = hints.get("videoSize")
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                size = None
            streams.append(
                _Stream(info_hash.lower(), file_idx, filename, size, _trackers(item.get("sources")))
            )
        except (KeyError, TypeError, AttributeError):
            continue
    return streams


def _trackers(sources: object) -> tuple[str, ...]:
    """The tracker URLs of a `sources` list. Its `dht:` entries and odd items are skipped."""
    if not isinstance(sources, list):
        return ()
    prefix = "tracker:"
    return tuple(
        source.removeprefix(prefix)
        for source in sources
        if isinstance(source, str) and source.startswith(prefix) and len(source) > len(prefix)
    )


def _title_filename(title: object) -> str | None:
    """Return the last title line before the stats line: the file name, else the torrent name.

    A season pack lists the file with its folder, so only the last path part is kept.
    """
    if not isinstance(title, str):
        return None
    name = None
    for line in title.splitlines():
        if any(mark in line for mark in _STATS_MARKS):
            break
        if line.strip():
            name = _basename(line.strip())
    return name or None


def _basename(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).name


def _normalize(name: str) -> str:
    return _basename(name).casefold()


def _match(streams: list[_Stream], filename: str | None, size: int | None) -> _Stream | None:
    found: list[_Stream] = []
    if filename is not None:
        wanted = _normalize(filename)
        found = [
            stream
            for stream in streams
            if stream.filename is not None
            and _normalize(stream.filename) == wanted
            and (size is None or stream.size is None or stream.size == size)
        ]
    if not found and size is not None:
        found = [stream for stream in streams if stream.size == size]
    distinct = {(stream.info_hash, stream.file_idx): stream for stream in found}
    return next(iter(distinct.values())) if len(distinct) == 1 else None
