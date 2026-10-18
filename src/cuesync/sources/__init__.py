"""The SubtitleSource protocol and the subtitle sources (OpenSubtitles)."""

from __future__ import annotations

from cuesync.sources.base import Candidate, SourceError, SubtitleRequest, SubtitleSource
from cuesync.sources.opensubtitles import OpenSubtitlesSource

__all__ = [
    "Candidate",
    "OpenSubtitlesSource",
    "SourceError",
    "SubtitleRequest",
    "SubtitleSource",
]
