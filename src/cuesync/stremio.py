"""Parsing of the values Stremio puts in addon request URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl

_VIDEO_ID = re.compile(r"(tt\d+)(?::(\d+):(\d+))?")


@dataclass(frozen=True)
class Extra:
    """The `extra` segment of a subtitles request. Missing or invalid values are None."""

    video_hash: str | None
    video_size: int | None
    filename: str | None


@dataclass(frozen=True)
class VideoId:
    """An IMDb id, with season and episode when the video is an episode."""

    imdb_id: str
    season: int | None
    episode: int | None


def raw_extra_segment(raw_path: bytes) -> str:
    """Return the undecoded `extra` text from a raw request path.

    Starlette percent-decodes path parameters, so an encoded `&`, `=` or `/` in a filename
    would be indistinguishable from a separator. The raw path keeps the escapes intact.
    """
    segment = raw_path.rsplit(b"/", 1)[-1].decode("utf-8", errors="replace")
    return segment.removesuffix(".json")


def parse_extra(extra: str) -> Extra:
    """Parse the `extra` text Stremio sends, such as
    `videoHash=8e24...&videoSize=1478453234&filename=Movie.2010.1080p.mkv`."""
    values = dict(parse_qsl(extra, keep_blank_values=True))
    size = values.get("videoSize", "")
    return Extra(
        video_hash=values.get("videoHash") or None,
        video_size=int(size) if size.isascii() and size.isdigit() else None,
        filename=values.get("filename") or None,
    )


def parse_video_id(id: str) -> VideoId:
    """Split `tt0903747:1:2` into IMDb id, season and episode. Raise ValueError if malformed."""
    match = _VIDEO_ID.fullmatch(id)
    if match is None:
        raise ValueError("malformed video id")
    imdb_id, season, episode = match.groups()
    return VideoId(
        imdb_id=imdb_id,
        season=int(season) if season is not None else None,
        episode=int(episode) if episode is not None else None,
    )
