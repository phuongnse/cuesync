"""Speech timelines from audio and from subtitle cues, and the offset between them."""

from __future__ import annotations

from cuesync.sync.offset import WindowOffset, estimate_offset
from cuesync.sync.speech import audio_speech, bridge_gaps, is_non_speech, subtitle_speech

__all__ = [
    "WindowOffset",
    "audio_speech",
    "bridge_gaps",
    "estimate_offset",
    "is_non_speech",
    "subtitle_speech",
]
