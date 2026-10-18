"""One correction for the whole subtitle, from the offsets measured in the audio windows.

The correction is `video time = a * subtitle time + b`. The speed ratio `a` is 1 or a common
frame-rate ratio, never an arbitrary fitted slope. Times are in milliseconds. These are pure
functions with no I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

import numpy as np

from cuesync.subtitles import Cue
from cuesync.sync.offset import SEARCH_S, WindowOffset, estimate_offset
from cuesync.sync.speech import subtitle_speech

NTSC = 24000 / 1001  # the real rate behind "23.976"
RATIOS = (1.0, 25 / NTSC, NTSC / 25, 24 / NTSC, NTSC / 24, 25 / 24, 24 / 25)
INLIER_MS = 500  # a window agrees when its shift is this close to the median shift
MIN_AGREEING = 3
AGREE_FRACTION = 0.75  # of the confident windows
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
    agreeing: int  # confident windows that fit
    residual_ms: float  # mean |b_i - b| over the agreeing windows


def _verdict(confident: int, agreeing: int, scale: float, offset: float) -> Verdict:
    if confident < MIN_AGREEING or agreeing < MIN_AGREEING or agreeing < AGREE_FRACTION * confident:
        return Verdict.FAILED
    if scale == 1.0 and abs(offset) <= UNCHANGED_MS:
        return Verdict.UNCHANGED
    return Verdict.SYNCED


def _failed(agreeing: int = 0, residual_ms: float = 0.0) -> Correction:
    return Correction(Verdict.FAILED, 1.0, 0, agreeing, residual_ms)


def fit_correction(offsets: Sequence[WindowOffset], ratios: Sequence[float] = RATIOS) -> Correction:
    """Fit `y = a*x + b` over the confident windows, with `a` taken from `ratios`.

    Here `y` is video time (the window centre) and `x` is subtitle time (`y - offset`). For
    each ratio the shifts `y - a*x` of the windows should agree. The ratio with the most
    agreeing windows wins, then the smallest residual, then the earlier one in `ratios`.
    """
    confident = [o for o in offsets if o.confident]
    if len(confident) < MIN_AGREEING:
        return _failed()
    y = np.array([o.window_center_s * 1000 for o in confident])
    x = y - np.array([o.offset_ms for o in confident], dtype=np.float64)

    best: tuple[int, float, float, float] | None = None  # agreeing, residual, a, b
    for a in ratios:
        shifts = y - a * x
        inliers = shifts[np.abs(shifts - np.median(shifts)) <= INLIER_MS]
        b = float(inliers.mean())
        residual = float(np.abs(inliers - b).mean())
        if best is None or (len(inliers), -residual) > (best[0], -best[1]):
            best = (len(inliers), residual, a, b)
    if best is None:
        return _failed()
    agreeing, residual, a, b = best
    verdict = _verdict(len(confident), agreeing, a, b)
    if verdict is Verdict.FAILED:
        return _failed(agreeing, residual)
    return Correction(verdict, a, round(b), agreeing, residual)


def align(
    windows: Sequence[tuple[float, np.ndarray]],
    cues: Sequence[Cue],
    *,
    ratios: Sequence[float] = RATIOS,
    search_s: float = SEARCH_S,
) -> Correction:
    """Find the correction that lines the cues up with the audio windows.

    Each window is `(start_s, speech timeline)`. For each ratio the cues are stretched
    first and the windows measured again, so a drift that would push late windows out of the
    search range is still found. The result is the ratio under which most windows agree.
    """
    best = _failed()
    for a in ratios:
        stretched = [Cue(round(c.start_ms * a), round(c.end_ms * a), c.text) for c in cues]
        subtitle = subtitle_speech(stretched)
        offsets = [
            estimate_offset(timeline, subtitle, start_s, search_s=search_s)
            for start_s, timeline in windows
        ]
        fit = fit_correction(offsets, ratios=(1.0,))
        if fit.verdict is Verdict.FAILED:
            continue
        verdict = _verdict(sum(o.confident for o in offsets), fit.agreeing, a, fit.offset_ms)
        candidate = replace(fit, verdict=verdict, scale=a)
        if best.verdict is Verdict.FAILED or (candidate.agreeing, -candidate.residual_ms) > (
            best.agreeing,
            -best.residual_ms,
        ):
            best = candidate
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
