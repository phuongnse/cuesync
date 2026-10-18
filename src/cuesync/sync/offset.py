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


def estimate_offset(
    audio: np.ndarray, subtitle: np.ndarray, start_s: float, *, search_s: float = SEARCH_S
) -> WindowOffset:
    """Find the shift that best lines the subtitle timeline up with one audio window.

    `audio` is the speech timeline of the window, whose first frame is at `start_s` in video
    time. `subtitle` is the speech timeline of the whole subtitle, with frame 0 at subtitle
    time 0. The shift is searched within `search_s` either side of zero.
    """
    n = len(audio)
    center = start_s + n / FRAME_RATE / 2
    audio = np.asarray(audio, dtype=np.float64)
    subtitle = np.asarray(subtitle, dtype=np.float64)
    min_speech = MIN_SPEECH_S * FRAME_RATE
    if n == 0 or audio.sum() < min_speech:
        return WindowOffset(center, 0, 0.0, False)

    start = round(start_s * FRAME_RATE)
    reach = round(search_s * FRAME_RATE)
    padded = np.zeros(n + 2 * reach)
    lo, hi = max(start - reach, 0), min(start + n + reach, len(subtitle))
    if hi > lo:
        padded[lo - (start - reach) : hi - (start - reach)] = subtitle[lo:hi]
    if padded.sum() < min_speech:
        return WindowOffset(center, 0, 0.0, False)

    lags = 2 * reach + 1
    corr = _correlate(audio, padded, lags)
    running = np.concatenate(([0.0], np.cumsum(padded)))
    covered = running[n : n + lags] - running[:lags]
    agree = (n - audio.sum() - covered + 2 * corr) / n

    best = int(np.argmax(agree))
    shift = reach - best  # audio frame i matches subtitle frame start + i - shift
    base = float(np.median(agree))
    gain = float(agree[best]) - base
    exclude = round(PEAK_EXCLUDE_S * FRAME_RATE)
    outside = np.abs(np.arange(lags) - best) > exclude
    second_gain = max(float(agree[outside].max()) - base, 0.0) if outside.any() else 0.0

    confident = gain > 0 and gain >= PEAK_RATIO * second_gain
    confident = confident and gain >= PEAK_SIGMAS * float(agree.std())
    if abs(shift) > exclude:
        confident = confident and agree[best] - agree[reach] >= MIN_IMPROVEMENT
    confident = confident and abs(shift) <= reach - exclude
    return WindowOffset(center, shift * (1000 // FRAME_RATE), float(agree[best]), bool(confident))
