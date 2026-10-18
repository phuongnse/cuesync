"""Tests for the global correction: the fit, the stretch-and-measure search and its use.

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
    Correction,
    Verdict,
    WindowOffset,
    align,
    apply_correction,
    audio_speech,
    estimate_offset,
    fit_correction,
    subtitle_speech,
)

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


def windows_for(a: float, b: float, centers=CENTERS) -> list[WindowOffset]:
    """Confident windows for the true correction y = a*x + b, with x = (y - b) / a."""
    out = []
    for c in centers:
        y = c * 1000.0
        out.append(WindowOffset(float(c), round(y - (y - b) / a), 0.9, True))
    return out


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


def runs(timeline: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) frames of each run of speech."""
    padded = np.concatenate(([0], (timeline > 0).astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(padded))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist(), strict=True))


def distort(cues: list[Cue], a: float, b: float) -> list[Cue]:
    """Subtitle cues that the correction y = a*x + b maps onto `cues`."""
    return [Cue(round((c.start_ms - b) / a), round((c.end_ms - b) / a), c.text) for c in cues]


def test_fit_pure_offset() -> None:
    result = fit_correction(windows_for(1.0, 3200))
    assert result.verdict is Verdict.SYNCED
    assert result.scale == 1.0
    assert abs(result.offset_ms - 3200) <= 10
    assert result.agreeing == 5


def test_fit_frame_rate_drift() -> None:
    result = fit_correction(windows_for(NTSC / 25, 0))
    assert result.verdict is Verdict.SYNCED
    assert result.scale == NTSC / 25
    assert abs(result.offset_ms) <= 50


def test_fit_drift_plus_offset() -> None:
    result = fit_correction(windows_for(NTSC / 25, 2000))
    assert result.verdict is Verdict.SYNCED
    assert result.scale == NTSC / 25
    assert abs(result.offset_ms - 2000) <= 50


@pytest.mark.parametrize("a", (25 / NTSC, 24 / NTSC, NTSC / 24, 25 / 24, 24 / 25))
def test_fit_finds_each_ratio(a: float) -> None:
    result = fit_correction(windows_for(a, 1000))
    assert result.verdict is Verdict.SYNCED
    assert result.scale == a


def test_fit_ignores_one_outlier() -> None:
    offsets = windows_for(NTSC / 25, 2000)
    offsets[2] = WindowOffset(3000.0, offsets[2].offset_ms + 40000, 0.9, True)
    result = fit_correction(offsets)
    assert result.verdict is Verdict.SYNCED
    assert result.scale == NTSC / 25
    assert abs(result.offset_ms - 2000) <= 50
    assert result.agreeing == 4


def test_fit_fails_when_windows_disagree() -> None:
    offsets = [
        WindowOffset(c, o, 0.9, True)
        for c, o in zip(CENTERS, (0, 0, 8000, 8000, 20000), strict=True)
    ]
    result = fit_correction(offsets)
    assert result == Correction(Verdict.FAILED, 1.0, 0, result.agreeing, result.residual_ms)


def test_fit_fails_when_two_of_five_disagree() -> None:
    offsets = windows_for(1.0, 3000)
    offsets[1] = WindowOffset(1800.0, 9000, 0.9, True)
    offsets[3] = WindowOffset(4200.0, -7000, 0.9, True)
    assert fit_correction(offsets).verdict is Verdict.FAILED


def test_fit_fails_with_fewer_than_three_confident() -> None:
    offsets = windows_for(1.0, 3000)
    offsets = [
        o if i < 2 else WindowOffset(o.window_center_s, 0, 0.1, False)
        for i, o in enumerate(offsets)
    ]
    assert fit_correction(offsets) == Correction(Verdict.FAILED, 1.0, 0, 0, 0.0)


def test_fit_ignores_unconfident_windows() -> None:
    offsets = windows_for(1.0, 3000)
    offsets.append(WindowOffset(100.0, 55000, 0.1, False))
    offsets.append(WindowOffset(200.0, -30000, 0.1, False))
    result = fit_correction(offsets)
    assert result.verdict is Verdict.SYNCED
    assert result.agreeing == 5
    assert abs(result.offset_ms - 3000) <= 10


def test_fit_already_in_sync_is_unchanged() -> None:
    result = fit_correction(windows_for(1.0, 40))
    assert result.verdict is Verdict.UNCHANGED
    assert result.scale == 1.0


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
    fit = fit_correction(plain)
    assert not (fit.verdict is Verdict.SYNCED and fit.scale == pytest.approx(a))


def test_align_fails_on_a_different_cut() -> None:
    rng = np.random.default_rng(8)
    full = blocky(rng, 3000 * FPS)
    other = blocky(rng, 3000 * FPS)
    cues = [Cue(s * 10, e * 10, "x") for s, e in runs(other)]
    starts = window_starts(3000, 5, 120)
    windows = [(s, full[round(s * FPS) : round((s + 120) * FPS)]) for s in starts]
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
