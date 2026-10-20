"""Speech timelines from audio and from subtitle cues, their offset, and the global correction."""

from __future__ import annotations

from cuesync.sync.correction import (
    ENGINE_VERSION,
    RATIOS,
    Correction,
    Verdict,
    align,
    apply_correction,
)
from cuesync.sync.offset import WindowOffset, estimate_offset
from cuesync.sync.speech import audio_speech, bridge_gaps, is_non_speech, subtitle_speech

__all__ = [
    "ENGINE_VERSION",
    "RATIOS",
    "Correction",
    "Verdict",
    "WindowOffset",
    "align",
    "apply_correction",
    "audio_speech",
    "bridge_gaps",
    "estimate_offset",
    "is_non_speech",
    "subtitle_speech",
]
