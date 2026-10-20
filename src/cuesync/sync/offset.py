"""Subtitle offset of one audio window, by FFT cross-correlation of speech timelines.

The offset is the amount to add to subtitle times so they match the audio, so a positive
value means the subtitles appear too early. Results are in 10 ms steps. These are pure
functions with no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from cuesync.sync.speech import FRAME_RATE

SEARCH_S = 60.0
MIN_SPEECH_S = 5.0  # speech needed on each side for an answer to mean anything
PEAK_RATIO = 1.5  # the best peak's gain over the median, relative to the next best peak's
PEAK_EXCLUDE_S = 2.0  # peaks closer than this to the best one belong to it
PEAK_SIGMAS = 4.5  # the best peak's gain over the median, in standard deviations of the scores
MIN_IMPROVEMENT = 0.02  # agreement gained over leaving the subtitles where they are


@dataclass(frozen=True)
class Peak:
    shift: int  # frames, positive means subtitles appear too early
    score: float  # agreement at the best shift
    sigmas: float  # (best - median) / std of the scores, 0 when std is 0
    confident: bool


@dataclass(frozen=True)
class WindowOffset:
    window_center_s: float  # video time
    offset_ms: int  # add to subtitle times to match the audio
    score: float  # fraction of frames where both timelines agree at the best shift, 0..1
    confident: bool


def _correlate(audio: np.ndarray, padded: np.ndarray, lags: int) -> np.ndarray:
    """c[m] = sum_i audio[i] * padded[i + m] for m in 0..lags-1."""
    size = 1 << (len(audio) + len(padded) - 1).bit_length()
    spectrum = np.conj(np.fft.rfft(audio, size)) * np.fft.rfft(padded, size)
    return np.rint(np.fft.irfft(spectrum, size)[:lags])


def agreement(
    audio: np.ndarray, subtitle: np.ndarray, start_s: float, *, search_s: float = SEARCH_S
) -> np.ndarray | None:
    """Fraction of frames where the two timelines agree, at every shift within `search_s`.

    `audio` is the speech timeline of the window, whose first frame is at `start_s` in video
    time. `subtitle` is the speech timeline of the whole subtitle, with frame 0 at subtitle
    time 0. Index m of the result is a shift of `reach - m` frames, with `reach = search_s`
    in frames. It is None when the window or the subtitle range has too little speech.
    """
    n = len(audio)
    audio = np.asarray(audio, dtype=np.float64)
    subtitle = np.asarray(subtitle, dtype=np.float64)
    min_speech = MIN_SPEECH_S * FRAME_RATE
    if n == 0 or audio.sum() < min_speech:
        return None

    start = round(start_s * FRAME_RATE)
    reach = round(search_s * FRAME_RATE)
    padded = np.zeros(n + 2 * reach)
    lo, hi = max(start - reach, 0), min(start + n + reach, len(subtitle))
    if hi > lo:
        padded[lo - (start - reach) : hi - (start - reach)] = subtitle[lo:hi]
    if padded.sum() < min_speech:
        return None

    lags = 2 * reach + 1
    corr = _correlate(audio, padded, lags)
    running = np.concatenate(([0.0], np.cumsum(padded)))
    covered = running[n : n + lags] - running[:lags]
    return (n - audio.sum() - covered + 2 * corr) / n


def find_peak(agree: np.ndarray, reach: int, *, peak_ratio: float, sigmas: float) -> Peak:
    """The best shift of an agreement curve, and whether it clearly stands out.

    The peak must beat the next best peak outside `PEAK_EXCLUDE_S` by `peak_ratio` (both
    counted above the median) and the median by `sigmas` standard deviations. A shift
    away from zero must also beat "no shift", and no shift may sit at the edge of the range.
    """
    lags = len(agree)
    best = int(np.argmax(agree))
    shift = reach - best  # audio frame i matches subtitle frame start + i - shift
    base = float(np.median(agree))
    gain = float(agree[best]) - base
    std = float(agree.std())
    exclude = round(PEAK_EXCLUDE_S * FRAME_RATE)
    outside = np.abs(np.arange(lags) - best) > exclude
    second_gain = max(float(agree[outside].max()) - base, 0.0) if outside.any() else 0.0

    confident = gain > 0 and gain >= peak_ratio * second_gain
    confident = confident and gain >= sigmas * std
    if abs(shift) > exclude:
        confident = confident and agree[best] - agree[reach] >= MIN_IMPROVEMENT
    confident = confident and abs(shift) <= reach - exclude
    return Peak(shift, float(agree[best]), gain / std if std > 0 else 0.0, bool(confident))


def estimate_offset(
    audio: np.ndarray, subtitle: np.ndarray, start_s: float, *, search_s: float = SEARCH_S
) -> WindowOffset:
    """Find the shift that best lines the subtitle timeline up with one audio window.

    The shift is searched within `search_s` either side of zero.
    """
    center = start_s + len(audio) / FRAME_RATE / 2
    agree = agreement(audio, subtitle, start_s, search_s=search_s)
    if agree is None:
        return WindowOffset(center, 0, 0.0, False)
    peak = find_peak(agree, round(search_s * FRAME_RATE), peak_ratio=PEAK_RATIO, sigmas=PEAK_SIGMAS)
    return WindowOffset(center, peak.shift * (1000 // FRAME_RATE), peak.score, peak.confident)
