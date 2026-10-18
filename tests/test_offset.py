from __future__ import annotations

import numpy as np

from cuesync.sync import WindowOffset, estimate_offset

FPS = 100
START_S = 300
WINDOW = 120 * FPS
TOTAL = 700 * FPS


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


def shift(timeline: np.ndarray, frames: int) -> np.ndarray:
    """Delay by `frames` (advance when negative), filling with zeros."""
    out = np.zeros_like(timeline)
    if frames >= 0:
        out[frames:] = timeline[: len(timeline) - frames]
    else:
        out[:frames] = timeline[-frames:]
    return out


def case(
    full: np.ndarray, frames: int, *, start_s: int = START_S, window: int = WINDOW
) -> tuple[np.ndarray, np.ndarray]:
    """Audio window of `full` and a subtitle timeline with subtitle[t] = full[t + frames]."""
    s = start_s * FPS
    return full[s : s + window], shift(full, -frames)


def test_constant_positive_shift_is_exact():
    full = blocky(np.random.default_rng(1), TOTAL)
    audio, subtitle = case(full, 347)
    result = estimate_offset(audio, subtitle, START_S)
    assert result.offset_ms == 3470
    assert result.confident
    assert result.score > 0.99
    assert result.window_center_s == 360.0


def test_constant_negative_shift_is_exact():
    full = blocky(np.random.default_rng(2), TOTAL)
    audio, subtitle = case(full, -1230)
    result = estimate_offset(audio, subtitle, START_S)
    assert result.offset_ms == -12300
    assert result.confident


def test_no_shift_is_confident_zero():
    full = blocky(np.random.default_rng(3), TOTAL)
    audio, subtitle = case(full, 0)
    assert estimate_offset(audio, subtitle, START_S) == WindowOffset(360.0, 0, 1.0, True)


def test_silent_audio_is_not_confident():
    subtitle = blocky(np.random.default_rng(4), TOTAL)
    result = estimate_offset(np.zeros(WINDOW, np.float32), subtitle, START_S)
    assert not result.confident


def test_silent_subtitle_is_not_confident():
    audio = blocky(np.random.default_rng(5), WINDOW)
    result = estimate_offset(audio, np.zeros(TOTAL, np.float32), START_S)
    assert not result.confident


def test_subtitle_without_speech_in_range_is_not_confident():
    rng = np.random.default_rng(6)
    audio = blocky(rng, WINDOW)
    subtitle = np.zeros(TOTAL, np.float32)
    subtitle[: 100 * FPS] = blocky(rng, 100 * FPS)
    assert not estimate_offset(audio, subtitle, START_S).confident


def test_empty_inputs_are_not_confident():
    empty = np.zeros(0, np.float32)
    assert not estimate_offset(empty, blocky(np.random.default_rng(7), TOTAL), 10).confident
    audio = blocky(np.random.default_rng(7), WINDOW)
    assert not estimate_offset(audio, empty, 10).confident


def test_periodic_timelines_are_not_confident():
    full = np.tile(np.r_[np.ones(200), np.zeros(200)].astype(np.float32), TOTAL // 400 + 1)
    audio, subtitle = case(full[:TOTAL], 123)
    assert not estimate_offset(audio, subtitle, START_S).confident


def test_independent_random_timelines_are_not_confident():
    for seed in range(5):
        rng = np.random.default_rng(100 + seed)
        audio = blocky(rng, WINDOW)
        subtitle = blocky(rng, TOTAL)
        assert not estimate_offset(audio, subtitle, START_S).confident, seed


def test_noisy_copy_is_found_within_50_ms():
    rng = np.random.default_rng(8)
    full = blocky(rng, TOTAL)
    edges = np.flatnonzero(np.diff(np.r_[0, full, 0]))
    runs = list(zip(edges[::2], edges[1::2], strict=True))
    noisy = np.zeros(TOTAL, np.float32)
    for begin, end in runs:
        if rng.random() < 0.1:
            continue
        begin += int(rng.integers(-15, 16))
        end += int(rng.integers(-15, 16))
        noisy[max(begin, 0) : max(end, 0)] = 1.0
    for _ in range(8):
        begin = int(rng.integers(0, TOTAL - FPS))
        noisy[begin : begin + FPS] = 1.0
    frames = -820
    s = START_S * FPS
    audio = full[s : s + WINDOW]
    result = estimate_offset(audio, shift(noisy, -frames), START_S)
    assert abs(result.offset_ms - frames * 10) <= 50
    assert result.confident


def test_shift_beyond_search_range_needs_a_wider_search():
    full = blocky(np.random.default_rng(9), TOTAL)
    audio, subtitle = case(full, 9000)
    assert not estimate_offset(audio, subtitle, START_S).confident
    wide = estimate_offset(audio, subtitle, START_S, search_s=120)
    assert wide.offset_ms == 90000
    assert wide.confident


def test_frame_rate_mismatch_shows_as_growing_offsets():
    ratio = 23.976 / 25
    full = blocky(np.random.default_rng(10), TOTAL)
    # A subtitle timed for 23.976 fps, played against a 25 fps video.
    subtitle = full[(np.arange(int(TOTAL * ratio)) / ratio).round().astype(int)]
    offsets = []
    for start_s in (120, 480):
        audio = full[start_s * FPS : (start_s + 30) * FPS]
        result = estimate_offset(audio, subtitle, start_s)
        expected = result.window_center_s * (1 - ratio)
        assert abs(result.offset_ms / 1000 - expected) < 0.5
        offsets.append(result.offset_ms)
    assert offsets[1] > offsets[0] + 10000
