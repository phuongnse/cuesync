"""Speech timelines from audio and from subtitle cues, their offset, and the global correction."""

from __future__ import annotations

from cuesync.sync.correction import (
    RATIOS,
    Correction,
    Verdict,
    align,
    apply_correction,
    fit_correction,
)
from cuesync.sync.offset import WindowOffset, estimate_offset
from cuesync.sync.speech import audio_speech, bridge_gaps, is_non_speech, subtitle_speech

__all__ = [
    "RATIOS",
    "Correction",
    "Verdict",
    "WindowOffset",
    "align",
    "apply_correction",
    "audio_speech",
    "bridge_gaps",
    "estimate_offset",
    "fit_correction",
    "is_non_speech",
    "subtitle_speech",
]
