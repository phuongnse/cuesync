"""One correction for the whole subtitle, from the agreement of all the audio windows.

The correction is `video time = a * subtitle time + b`. The speed ratio `a` is 1 or a common
frame-rate ratio, never an arbitrary fitted slope. Times are in milliseconds. These are pure
functions with no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from cuesync.subtitles import Cue
from cuesync.sync.offset import SEARCH_S, agreement, find_peak
from cuesync.sync.speech import FRAME_RATE, subtitle_speech

NTSC = 24000 / 1001  # the real rate behind "23.976"
RATIOS = (1.0, 25 / NTSC, NTSC / 25, 24 / NTSC, NTSC / 24, 25 / 24, 24 / 25)
MIN_WINDOWS = 3  # windows with enough speech, or one cannot tell a different cut from a match
COMBINED_PEAK_RATIO = 2.0  # the combined peak's gain over the next best peak's
COMBINED_SIGMAS = 4.0  # the combined peak's gain over the median, in standard deviations
ENGINE_VERSION = 2  # bump when a change makes stored sync results stale
UNCHANGED_MS = 100  # a shift this small is not worth a re-timed copy


class Verdict(StrEnum):
    SYNCED = "synced"
    UNCHANGED = "unchanged"
    FAILED = "failed"


@dataclass(frozen=True)
class Correction:
    verdict: Verdict
    scale: float  # a
    offset_ms: int  # b
    agreeing: int  # windows in the combined curve
    residual_ms: float  # mean |own best shift - chosen shift| over those windows


def _failed() -> Correction:
    return Correction(Verdict.FAILED, 1.0, 0, 0, 0.0)


def align(
    windows: Sequence[tuple[float, np.ndarray]],
    cues: Sequence[Cue],
    *,
    ratios: Sequence[float] = RATIOS,
    search_s: float = SEARCH_S,
) -> Correction:
    """Find the correction that lines the cues up with the audio windows.

    Each window is `(start_s, speech timeline)`. For each ratio the cues are stretched
    first, so a drift that would push late windows out of the search range is still found.
    The agreement curves of the windows are summed, weighted by window length, and the
    peak of that one curve gives the shift. The ratio whose peak stands out most wins, and
    when no ratio has a clear peak, or fewer than `MIN_WINDOWS` windows have enough speech,
    the result is failed.
    """
    reach = round(search_s * FRAME_RATE)
    best = _failed()
    best_sigmas = 0.0
    for a in ratios:
        stretched = [Cue(round(c.start_ms * a), round(c.end_ms * a), c.text) for c in cues]
        subtitle = subtitle_speech(stretched)
        curves, weights = [], []
        for start_s, timeline in windows:
            curve = agreement(timeline, subtitle, start_s, search_s=search_s)
            if curve is not None:
                curves.append(curve)
                weights.append(len(timeline))
        if len(curves) < MIN_WINDOWS:
            continue
        combined = np.average(curves, axis=0, weights=weights)
        peak = find_peak(combined, reach, peak_ratio=COMBINED_PEAK_RATIO, sigmas=COMBINED_SIGMAS)
        if not peak.confident or (
            best.verdict is not Verdict.FAILED and peak.sigmas <= best_sigmas
        ):
            continue
        offset_ms = peak.shift * (1000 // FRAME_RATE)
        own = np.array([reach - int(np.argmax(c)) for c in curves])
        residual_ms = float(np.abs(own - peak.shift).mean()) * (1000 // FRAME_RATE)
        verdict = (
            Verdict.UNCHANGED if a == 1.0 and abs(offset_ms) <= UNCHANGED_MS else Verdict.SYNCED
        )
        best = Correction(verdict, a, offset_ms, len(curves), residual_ms)
        best_sigmas = peak.sigmas
    return best


def apply_correction(cues: Sequence[Cue], correction: Correction) -> tuple[Cue, ...]:
    """Map cue times through the correction, clamp at 0 and drop cues that end at or before 0."""
    if correction.verdict is Verdict.FAILED:
        raise ValueError("a failed correction cannot be applied")
    if correction.verdict is Verdict.UNCHANGED:
        return tuple(cues)

    def move(t: int) -> int:
        return max(0, round(correction.scale * t + correction.offset_ms))

    moved = (Cue(move(c.start_ms), move(c.end_ms), c.text) for c in cues)
    return tuple(c for c in moved if c.end_ms > 0)
