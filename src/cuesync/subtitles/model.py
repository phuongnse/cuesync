"""The subtitle model shared by every source and the sync engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Cue:
    """One timed piece of text. Times are in milliseconds."""

    start_ms: int
    end_ms: int
    text: str  # plain text, lines joined by "\n"


@dataclass(frozen=True)
class Subtitle:
    """A parsed subtitle file and where it came from."""

    cues: tuple[Cue, ...]
    language: str  # 3-letter code
    source: str  # e.g. "subdl"
    source_id: str
    release_name: str
    hash_match: bool = False


class SubtitleError(ValueError):
    """The data is not a usable subtitle file."""
