"""The SubtitleSource protocol and the subtitle source (SubDL)."""

from __future__ import annotations

from cuesync.sources.base import Candidate, SourceError, SubtitleRequest, SubtitleSource
from cuesync.sources.subdl import SubDLSource

__all__ = [
    "Candidate",
    "SourceError",
    "SubDLSource",
    "SubtitleRequest",
    "SubtitleSource",
]
