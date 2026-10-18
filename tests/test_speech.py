from __future__ import annotations

import numpy as np
import pytest

from cuesync.audio import SAMPLE_RATE
from cuesync.subtitles import Cue
from cuesync.sync import audio_speech, bridge_gaps, is_non_speech, subtitle_speech

BURSTS = [(1.0, 2.5), (4.0, 5.0), (7.0, 8.5)]
DURATION = 10


def synth_pcm() -> bytes:
    """Noise at about -60 dBFS with voiced, syllable-modulated bursts."""
    rng = np.random.default_rng(0)
    t = np.arange(DURATION * SAMPLE_RATE) / SAMPLE_RATE
    signal = rng.standard_normal(len(t)) * 0.001
    voiced = sum(np.sin(2 * np.pi * 140 * k * t) / k for k in range(1, 21))
    voiced = voiced * (0.6 + 0.4 * np.cos(2 * np.pi * 4 * t))
    voiced = voiced / np.abs(voiced).max() * 0.3
    for start, end in BURSTS:
        sl = slice(int(start * SAMPLE_RATE), int(end * SAMPLE_RATE))
        signal[sl] += voiced[sl]
    return (np.clip(signal, -1, 1) * 32767).astype("<i2").tobytes()


def inside(frames: np.ndarray, start: float, end: float, margin: float = 0.1) -> np.ndarray:
    return frames[int((start + margin) * 100) : int((end - margin) * 100)]


def test_audio_speech_finds_bursts_and_gaps() -> None:
    timeline = audio_speech(synth_pcm())
    assert timeline.dtype == np.float32
    assert len(timeline) == DURATION * 100
    burst = np.concatenate([inside(timeline, s, e) for s, e in BURSTS])
    assert burst.mean() >= 0.9
    edges = [0.0] + [x for b in BURSTS for x in b] + [float(DURATION)]
    gaps = np.concatenate(
        [inside(timeline, a, b) for a, b in zip(edges[::2], edges[1::2], strict=True)]
    )
    assert gaps.mean() <= 0.05


@pytest.mark.parametrize("size", [0, 319, 320, 641, 3201])
def test_audio_speech_length(size: int) -> None:
    assert len(audio_speech(bytes(size))) == size // 320


def test_audio_speech_empty_is_empty_array() -> None:
    out = audio_speech(b"")
    assert out.shape == (0,)
    assert out.dtype == np.float32


def test_audio_speech_silence_is_all_zeros() -> None:
    assert not audio_speech(bytes(320 * 100)).any()


def test_bridge_gaps_fills_short_and_keeps_long_gaps() -> None:
    timeline = np.array([1] + [0] * 15 + [1] + [0] * 30 + [1], dtype=np.float32)
    out = bridge_gaps(timeline, 20)
    assert out[:17].all()
    assert not out[17:47].any()
    assert out[47] == 1


def test_bridge_gaps_keeps_edge_zeros_and_input() -> None:
    timeline = np.array([0] * 5 + [1, 0, 1] + [0] * 5, dtype=np.float32)
    original = timeline.copy()
    out = bridge_gaps(timeline, 20)
    assert out.tolist() == [0] * 5 + [1, 1, 1] + [0] * 5
    assert np.array_equal(timeline, original)


def test_bridge_gaps_without_speech() -> None:
    assert not bridge_gaps(np.zeros(10, np.float32), 20).any()
    assert bridge_gaps(np.zeros(0, np.float32), 20).shape == (0,)


def test_subtitle_speech_merges_overlapping_cues() -> None:
    timeline = subtitle_speech([Cue(1000, 2500, "a"), Cue(2000, 3005, "b")])
    assert timeline.dtype == np.float32
    assert len(timeline) == 301
    assert np.flatnonzero(timeline).tolist() == list(range(100, 301))


def test_subtitle_speech_without_cues() -> None:
    assert subtitle_speech([]).shape == (0,)


def test_subtitle_speech_frames_pads_and_truncates() -> None:
    cues = [Cue(1000, 2000, "a")]
    padded = subtitle_speech(cues, frames=500)
    assert len(padded) == 500
    assert np.flatnonzero(padded).tolist() == list(range(100, 200))
    truncated = subtitle_speech(cues, frames=150)
    assert np.flatnonzero(truncated).tolist() == list(range(100, 150))


def test_subtitle_speech_clips_negative_start_and_skips_empty_cues() -> None:
    cues = [Cue(-500, 200, "a"), Cue(1000, 1000, "b"), Cue(-300, -100, "c")]
    timeline = subtitle_speech(cues, frames=50)
    assert np.flatnonzero(timeline).tolist() == list(range(20))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("♪ la la ♪", True),
        ("♫ Song", True),
        ("[music]", True),
        ("(APPLAUSE)", True),
        ("- [laughs]\n- (sighs)", True),
        ("- [laughs]\n- Hello", False),
        ("[laughs]\nHello", False),
        ("(sighs) Hello", False),
        ("Hello", False),
        ("", False),
    ],
)
def test_is_non_speech(text: str, expected: bool) -> None:
    assert is_non_speech(text) is expected


def test_subtitle_speech_skip_non_speech() -> None:
    for text in ("♪ la la ♪", "[music]"):
        cues = [Cue(0, 1000, text)]
        assert not subtitle_speech(cues).any()
        assert subtitle_speech(cues, skip_non_speech=False).sum() == 100
    assert subtitle_speech([Cue(0, 1000, "[laughs]\nHello")]).sum() == 100
