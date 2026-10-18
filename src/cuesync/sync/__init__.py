"""Speech timelines from audio and from subtitle cues, the signals the sync step compares."""

from __future__ import annotations

from cuesync.sync.speech import audio_speech, bridge_gaps, is_non_speech, subtitle_speech

__all__ = [
    "audio_speech",
    "bridge_gaps",
    "is_non_speech",
    "subtitle_speech",
]
