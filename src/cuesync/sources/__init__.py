"""The SubtitleSource protocol and the subtitle sources (OpenSubtitles, SubDL)."""

from __future__ import annotations

from cuesync.sources.base import Candidate, SourceError, SubtitleRequest, SubtitleSource
from cuesync.sources.opensubtitles import OpenSubtitlesSource
from cuesync.sources.subdl import SubDLSource

__all__ = [
    "Candidate",
    "OpenSubtitlesSource",
    "SourceError",
    "SubDLSource",
    "SubtitleRequest",
    "SubtitleSource",
]
