"""Speech timelines at 100 Hz, from audio and from subtitle cues.

Both timelines are float32 arrays of 0.0 and 1.0. Frame i covers i*10 ms to (i+1)*10 ms.
These are pure functions with no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise

import numpy as np
import webrtcvad

from cuesync.audio import SAMPLE_RATE
from cuesync.subtitles import Cue

FRAME_MS = 10
FRAME_RATE = 1000 // FRAME_MS
MAX_GAP_MS = 200
VAD_MODE = 3  # the strictest WebRTC setting, as ffsubsync uses

_FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 16-bit mono PCM
_MUSIC_MARKS = "♪♫"


def bridge_gaps(timeline: np.ndarray, max_gap: int) -> np.ndarray:
    """Return a copy with runs of at most `max_gap` zeros between speech runs set to 1."""
    out = np.array(timeline, dtype=np.float32)
    speech = np.flatnonzero(out > 0)
    if len(speech) < 2:
        return out
    for before, after in pairwise(speech):
        if 1 < after - before <= max_gap + 1:
            out[before + 1 : after] = 1.0
    return out


def audio_speech(
    samples: bytes, *, mode: int = VAD_MODE, max_gap_ms: int = MAX_GAP_MS
) -> np.ndarray:
    """Speech timeline of 16 kHz mono 16-bit PCM, with short pauses bridged.

    A trailing partial frame is dropped.
    """
    count = len(samples) // _FRAME_BYTES
    out = np.zeros(count, np.float32)
    if count == 0:
        return out
    vad = webrtcvad.Vad(mode)
    for i in range(count):
        frame = samples[i * _FRAME_BYTES : (i + 1) * _FRAME_BYTES]
        out[i] = vad.is_speech(frame, SAMPLE_RATE)
    return bridge_gaps(out, max_gap_ms // FRAME_MS)


def _line_is_non_speech(line: str) -> bool:
    line = line.strip()
    if line.startswith("-"):
        line = line[1:].strip()
    if not line:
        return False
    if line[0] in _MUSIC_MARKS or line[-1] in _MUSIC_MARKS:
        return True
    return (line[0], line[-1]) in {("[", "]"), ("(", ")")}


def is_non_speech(text: str) -> bool:
    """True when every line is a song lyric (♪ or ♫) or a bracketed description."""
    lines = [line for line in text.split("\n") if line.strip()]
    return bool(lines) and all(_line_is_non_speech(line) for line in lines)


def subtitle_speech(
    cues: Sequence[Cue], frames: int | None = None, *, skip_non_speech: bool = True
) -> np.ndarray:
    """Speech timeline of subtitle cues, starting at 0 ms of the subtitle file.

    `frames` pads with zeros or truncates. By default it ends at the last cue.
    """
    if frames is None:
        frames = -(-max((c.end_ms for c in cues), default=0) // FRAME_MS)
    out = np.zeros(max(frames, 0), np.float32)
    for cue in cues:
        if cue.end_ms <= max(cue.start_ms, 0):
            continue
        if skip_non_speech and is_non_speech(cue.text):
            continue
        out[max(cue.start_ms, 0) // FRAME_MS : (cue.end_ms + FRAME_MS - 1) // FRAME_MS] = 1.0
    return out
