"""Tests for the global correction: the combined curve, the stretch-and-measure search, its use.

The end-to-end test generates speech-like audio with ffmpeg and runs the whole chain.
"""

from __future__ import annotations

import asyncio
import statistics
import subprocess
from pathlib import Path

import numpy as np
import pytest

from cuesync.audio import extract_windows, window_starts
from cuesync.subtitles import Cue
from cuesync.sync import (
    RATIOS,
    Correction,
    Verdict,
    align,
    apply_correction,
    audio_speech,
    estimate_offset,
    subtitle_speech,
)
from cuesync.sync.correction import COMBINED_SIGMAS

FPS = 100
NTSC = 24000 / 1001
CENTERS = (600, 1800, 3000, 4200, 5400)

# Speech-like harmonics gated on and off in pseudo-random half seconds, so it is not periodic.
SPEECH = (
    "aevalsrc='0.3*gt(mod(abs(sin(floor(2*t)*12.9898)*43758.5453),1),0.5)"
    "*(0.6+0.4*cos(8*PI*t))*(sin(2*PI*140*t)+sin(2*PI*280*t)/2+sin(2*PI*420*t)/3"
    "+sin(2*PI*560*t)/4+sin(2*PI*700*t)/5+sin(2*PI*840*t)/6)+0.001*(random(0)-0.5)'"
    ":s=16000:d=600"
)


def make_windows(
    full: np.ndarray, starts: list[float], length_s: float
) -> list[tuple[float, np.ndarray]]:
    return [(s, full[round(s * FPS) : round((s + length_s) * FPS)]) for s in starts]


def blocky(rng: np.random.Generator, frames: int) -> np.ndarray:
    """Speech runs of 1-4 s separated by gaps of 0.5-3 s."""
    out = np.zeros(frames, np.float32)
    i = 0
    while i < frames:
        i += int(rng.uniform(0.5, 3.0) * FPS)
        run = int(rng.uniform(1.0, 4.0) * FPS)
        out[i : i + run] = 1.0
        i += run
    return out


def dense(rng: np.random.Generator, frames: int) -> np.ndarray:
    """Speech runs of 2-8 s separated by gaps of 0.6-2.4 s, about 77% speech like a sitcom."""
    out = np.zeros(frames, np.float32)
    i = 0
    while i < frames:
        i += int(rng.uniform(0.6, 2.4) * FPS)
        run = int(rng.uniform(2.0, 8.0) * FPS)
        out[i : i + run] = 1.0
        i += run
    return out


def runs(timeline: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) frames of each run of speech."""
    padded = np.concatenate(([0], (timeline > 0).astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(padded))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist(), strict=True))


def distort(cues: list[Cue], a: float, b: float) -> list[Cue]:
    """Subtitle cues that the correction y = a*x + b maps onto `cues`."""
    return [Cue(round((c.start_ms - b) / a), round((c.end_ms - b) / a), c.text) for c in cues]


def test_align_finds_drift_beyond_the_search_range() -> None:
    a, b = NTSC / 25, 1500
    total = 7200
    full = blocky(np.random.default_rng(7), total * FPS)
    cues = distort([Cue(s * 10, e * 10, "x") for s, e in runs(full)], a, b)
    starts = window_starts(total, 5, 120)
    windows = [(s, full[round(s * FPS) : round((s + 120) * FPS)]) for s in starts]

    result = align(windows, cues)
    assert result.verdict is Verdict.SYNCED
    assert result.scale == pytest.approx(NTSC / 25)
    assert abs(result.offset_ms - b) <= 50

    # Measured once, without stretching, the late windows are out of range.
    subtitle = subtitle_speech(cues)
    plain = [estimate_offset(t, subtitle, s) for s, t in windows]
    assert sum(o.confident for o in plain) < len(plain)


def test_align_fails_on_a_different_cut() -> None:
    rng = np.random.default_rng(8)
    full = blocky(rng, 3000 * FPS)
    other = blocky(rng, 3000 * FPS)
    cues = [Cue(s * 10, e * 10, "x") for s, e in runs(other)]
    starts = window_starts(3000, 5, 120)
    windows = [(s, full[round(s * FPS) : round((s + 120) * FPS)]) for s in starts]
    assert align(windows, cues).verdict is Verdict.FAILED


def blocky_case(seed: int, a: float, b: float, count: int = 5):
    full = blocky(np.random.default_rng(seed), 3600 * FPS)
    cues = distort([Cue(s * 10, e * 10, "x") for s, e in runs(full)], a, b)
    return full, cues, window_starts(3600, count, 120)


@pytest.mark.parametrize("a", RATIOS[1:])
def test_align_finds_each_ratio(a: float) -> None:
    full, cues, starts = blocky_case(11, a, 1000)
    result = align(make_windows(full, starts, 120), cues)
    assert result.verdict is Verdict.SYNCED
    assert result.scale == pytest.approx(a)
    assert abs(result.offset_ms - 1000) <= 50


def test_align_reports_the_winning_peaks_strength_as_its_confidence() -> None:
    full, cues, starts = blocky_case(11, 25 / NTSC, 1000)
    result = align(make_windows(full, starts, 120), cues)
    assert result.verdict is Verdict.SYNCED
    assert result.confidence > COMBINED_SIGMAS
    other = align(
        make_windows(full, starts, 120), [Cue(c.start_ms + 7, c.end_ms, "x") for c in cues]
    )
    assert other.confidence != result.confidence
    assert align(make_windows(full, starts, 120)[:2], cues).confidence == 0.0


def test_align_small_shift_is_unchanged() -> None:
    full, cues, starts = blocky_case(12, 1.0, 40)
    result = align(make_windows(full, starts, 120), cues)
    assert result.verdict is Verdict.UNCHANGED
    assert result.scale == 1.0


def test_align_ignores_one_unrelated_window() -> None:
    full, cues, starts = blocky_case(13, 1.0, 3000)
    windows = make_windows(full, starts, 120)
    other = blocky(np.random.default_rng(99), 120 * FPS)
    windows[2] = (windows[2][0], other)
    result = align(windows, cues)
    assert result.verdict is Verdict.SYNCED
    assert abs(result.offset_ms - 3000) <= 50
    assert result.agreeing == 5


def test_align_fails_with_two_windows() -> None:
    full, cues, starts = blocky_case(14, 1.0, 3000, count=2)
    assert align(make_windows(full, starts, 120), cues).verdict is Verdict.FAILED


@pytest.mark.parametrize("seed", (0, 1, 6))
def test_align_syncs_when_no_window_is_confident_alone(seed: int) -> None:
    rng = np.random.default_rng(seed)
    frames = 2000 * FPS
    full = dense(rng, frames)
    assert 0.7 < full.mean() < 0.8

    # The subtitle misses some lines and times the others loosely, and the audio has laughs.
    cues = []
    for s, e in runs(full):
        if rng.random() < 0.3:
            continue
        s, e = s + int(rng.uniform(-0.3, 0.3) * FPS), e + int(rng.uniform(-0.3, 0.3) * FPS)
        if e > s >= 0:
            cues.append(Cue(s * 10 - 4400, e * 10 - 4400, "x"))
    cues = [c for c in cues if c.end_ms > 0]
    audio = full.copy()
    for _ in range(300):
        p = int(rng.uniform(0, frames))
        audio[p : p + int(rng.uniform(0.5, 2.0) * FPS)] = 1.0

    windows = make_windows(audio, [300, 900, 1500], 60)
    subtitle = subtitle_speech(cues)
    assert not any(estimate_offset(t, subtitle, s).confident for s, t in windows)

    result = align(windows, cues)
    assert result.verdict is Verdict.SYNCED
    assert result.scale == 1.0
    assert abs(result.offset_ms - 4400) <= 100
    assert result.agreeing == 3


def test_align_fails_when_the_shift_jumps_between_windows() -> None:
    full = dense(np.random.default_rng(3), 2000 * FPS)
    starts = [300, 900, 1500]
    cues: list[Cue] = []
    for start, shift in zip(starts, (0, 12, 24), strict=True):
        for s, e in runs(full[start * FPS : (start + 120) * FPS]):
            base = (start - shift) * 1000  # the audio is `shift` seconds later than the cues
            cues.append(Cue(s * 10 + base, e * 10 + base, "x"))
    windows = make_windows(full, starts, 120)
    assert align(windows, cues).verdict is Verdict.FAILED


@pytest.mark.parametrize("seed", range(5))
def test_align_fails_on_unrelated_dense_cues(seed: int) -> None:
    rng = np.random.default_rng(100 + seed)
    full = dense(rng, 2000 * FPS)
    cues = [Cue(s * 10, e * 10, "x") for s, e in runs(dense(rng, 2000 * FPS))]
    windows = make_windows(full, [300, 900, 1500], 60)
    assert align(windows, cues).verdict is Verdict.FAILED


CUES = [Cue(1000, 2000, "a"), Cue(3000, 4001, "b"), Cue(10_000, 12_000, "c")]


def test_apply_maps_times_through_the_correction() -> None:
    correction = Correction(Verdict.SYNCED, 25 / NTSC, 500, 5, 0.0)
    out = apply_correction(CUES, correction)
    assert [c.text for c in out] == ["a", "b", "c"]
    assert out[0].start_ms == round(1000 * 25 / NTSC + 500)
    assert out[1].end_ms == round(4001 * 25 / NTSC + 500)
    assert out[2].start_ms == round(10_000 * 25 / NTSC + 500)


def test_apply_clamps_at_zero_and_drops_cues_ending_before_it() -> None:
    correction = Correction(Verdict.SYNCED, 1.0, -3500, 5, 0.0)
    out = apply_correction(CUES, correction)
    assert out == (Cue(0, 501, "b"), Cue(6500, 8500, "c"))


def test_apply_drops_a_cue_ending_exactly_at_zero() -> None:
    correction = Correction(Verdict.SYNCED, 1.0, -2000, 5, 0.0)
    assert [c.text for c in apply_correction(CUES, correction)] == ["b", "c"]


def test_apply_unchanged_returns_the_input() -> None:
    correction = Correction(Verdict.UNCHANGED, 1.0, 40, 5, 0.0)
    assert apply_correction(CUES, correction) == tuple(CUES)


def test_apply_failed_raises() -> None:
    with pytest.raises(ValueError, match="failed"):
        apply_correction(CUES, Correction(Verdict.FAILED, 1.0, 0, 0, 0.0))


@pytest.fixture(scope="session")
def speech_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("speech") / "speech.mkv"
    subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SPEECH),
            *("-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(path)),
        ],
        check=True,
    )
    return path


def test_end_to_end_stretched_and_shifted_subtitle(speech_file: Path) -> None:
    pcm = subprocess.run(
        [
            *("ffmpeg", "-v", "error", "-i", str(speech_file)),
            *("-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"),
        ],
        check=True,
        capture_output=True,
    ).stdout
    reference = [Cue(s * 10, e * 10, "line") for s, e in runs(audio_speech(pcm))]
    assert len(reference) > 50

    a, b = 25 / NTSC, 2000
    cues = distort(reference, a, b)
    extracted = asyncio.run(extract_windows(str(speech_file), 600, count=5, length_s=60, edge_s=30))
    windows = [(w.start_s, audio_speech(w.samples)) for w in extracted]

    result = align(windows, cues)
    assert result.verdict is Verdict.SYNCED
    assert result.scale == a
    assert abs(result.offset_ms - b) <= 100

    fixed = apply_correction(cues, result)
    errors = [abs(f.start_ms - r.start_ms) for f, r in zip(fixed, reference, strict=False)]
    assert len(fixed) >= len(reference) - 2
    assert statistics.median(errors) <= 100
