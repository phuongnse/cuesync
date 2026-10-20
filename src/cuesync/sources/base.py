"""The interface every subtitle source implements."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from cuesync.subtitles import Subtitle


@dataclass(frozen=True)
class SubtitleRequest:
    """What Stremio is playing and which subtitles the user wants."""

    imdb_id: str  # "tt0903747"
    season: int | None
    episode: int | None
    languages: tuple[str, ...]  # 3-letter
    video_hash: str | None = None
    video_size: int | None = None
    filename: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A subtitle a source offers, before it is downloaded."""

    source: str
    source_id: str
    language: str  # 3-letter, the user's code
    release_name: str
    hash_match: bool = False
    downloads: int = 0  # how often the source says the file was downloaded, 0 when unknown


class SourceError(Exception):
    """A source could not deliver. The message is readable and never holds secrets."""


class SubtitleSource(Protocol):
    name: str

    async def search(self, request: SubtitleRequest) -> list[Candidate]:
        """Find candidates. Failures are logged and give an empty list."""
        ...

    async def download(self, candidate: Candidate) -> Subtitle:
        """Fetch and parse a candidate. Raises SourceError when that fails."""
        ...
