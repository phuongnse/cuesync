"""Subtitle model, decoding, SRT/VTT/ASS parsing, SRT writing and zip picking."""

from __future__ import annotations

from cuesync.subtitles.archive import pick_from_zip
from cuesync.subtitles.encoding import decode_bytes
from cuesync.subtitles.formats import parse_bytes, parse_text, write_srt
from cuesync.subtitles.model import Cue, Subtitle, SubtitleError

__all__ = [
    "Cue",
    "Subtitle",
    "SubtitleError",
    "decode_bytes",
    "parse_bytes",
    "parse_text",
    "pick_from_zip",
    "write_srt",
]
