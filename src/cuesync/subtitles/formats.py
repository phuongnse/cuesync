"""Parsing of SRT, WebVTT and ASS/SSA subtitles into cues, and writing of SRT."""

from __future__ import annotations

import re
import warnings
from collections.abc import Iterable

import pysubs2

from cuesync.subtitles.encoding import decode_bytes
from cuesync.subtitles.model import Cue, SubtitleError

_FORMATS = {"srt", "vtt", "ass", "ssa"}
_TAG = re.compile(r"<[^>]*>")
_DRAWING = re.compile(r"\\p[1-9]")


def _drop_swallowed_cues(lines: list[str]) -> list[str]:
    """Cut a cue's text where a cue with a broken start time was swallowed into it."""
    for index, line in enumerate(lines):
        if "-->" in line:
            keep = index - 1 if index > 0 and lines[index - 1].strip().isdigit() else index
            return lines[:keep]
    return lines


def parse_text(text: str) -> list[Cue]:
    """Parse SRT, WebVTT, ASS or SSA text into plain-text cues sorted by time.

    Formatting is removed. Empty, comment, drawing and zero-length cues are skipped.
    Raises SubtitleError when the format is not recognized or no usable cue remains.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            subs = pysubs2.SSAFile.from_string(text)
    except Exception as exc:  # pysubs2 raises several unrelated error types
        raise SubtitleError("unrecognized subtitle format") from exc
    if subs.format not in _FORMATS:
        raise SubtitleError("unrecognized subtitle format")
    cues: list[Cue] = []
    for event in subs.events:
        if event.is_comment or _DRAWING.search(event.text):
            continue
        raw_lines = _drop_swallowed_cues(event.plaintext.splitlines())
        lines = (_TAG.sub("", line).strip() for line in raw_lines)
        body = "\n".join(line for line in lines if line)
        if not body or event.end <= event.start:
            continue
        cues.append(Cue(int(event.start), int(event.end), body))
    if not cues:
        raise SubtitleError("no usable cues")
    cues.sort(key=lambda cue: (cue.start_ms, cue.end_ms))
    return cues


def parse_bytes(data: bytes, language: str | None = None) -> list[Cue]:
    """Decode subtitle bytes (see `decode_bytes`) and parse them (see `parse_text`)."""
    return parse_text(decode_bytes(data, language))


def _timestamp(ms: int) -> str:
    ms = max(ms, 0)
    seconds, millis = divmod(ms, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def write_srt(cues: Iterable[Cue]) -> bytes:
    """Write cues as UTF-8 SRT without a BOM, with LF line endings."""
    ordered = sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms))
    blocks = [
        f"{number}\n{_timestamp(cue.start_ms)} --> {_timestamp(cue.end_ms)}\n{cue.text}\n\n"
        for number, cue in enumerate(ordered, start=1)
    ]
    return "".join(blocks).encode("utf-8")
