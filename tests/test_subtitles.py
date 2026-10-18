"""Tests for subtitle decoding, parsing, SRT writing and zip picking."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from cuesync.subtitles import (
    Cue,
    SubtitleError,
    decode_bytes,
    parse_bytes,
    parse_text,
    pick_from_zip,
    write_srt,
)

SUBS = Path(__file__).parent / "fixtures" / "subs"

ENCODED = [
    ("eng.utf8bom.srt", "eng", "eng"),
    ("eng.utf16le.srt", "eng", "eng"),
    ("eng.utf16be.srt", "eng", "eng"),
    ("vie.cp1258.srt", "vie", "vie"),
    ("fre.cp1252.srt", "fre", "fre"),
    ("pol.cp1250.srt", "pol", "pol"),
    ("rus.cp1251.srt", "rus", "rus"),
    ("ara.cp1256.srt", "ara", "ara"),
    ("chi.gb18030.srt", "chi", "chi"),
    ("zht.big5.srt", "zht", "zht"),
]

SAMPLE_CUES = [
    Cue(1000, 3500, "Hello there."),
    Cue(4000, 6000, "Red and plain\non two lines."),
    Cue(7200, 9800, "Third cue."),
    Cue(62030, 64560, "Last one, after a minute."),
]


def read(name: str) -> bytes:
    return (SUBS / name).read_bytes()


@pytest.mark.parametrize(("name", "reference", "language"), ENCODED)
def test_encoded_fixture_matches_reference(name: str, reference: str, language: str) -> None:
    expected = parse_bytes(read(f"{reference}.srt"), language)
    assert len(expected) == 10
    assert parse_bytes(read(name), language) == expected


def test_vietnamese_cp1258_has_correct_diacritics() -> None:
    text = decode_bytes(read("vie.cp1258.srt"), "vie")
    assert "Tiếng Việt" in text
    assert "người" in text
    assert "\r" not in text


def test_decode_strips_bom_and_normalizes_newlines() -> None:
    assert decode_bytes(b"\xef\xbb\xbfa\r\nb\rc") == "a\nb\nc"


def test_decode_uses_language_hint_for_short_text() -> None:
    assert decode_bytes("déjà vu".encode("cp1252"), "fre") == "déjà vu"
    assert decode_bytes("привет".encode("cp1251"), "rus") == "привет"


@pytest.mark.parametrize("name", ["sample.srt", "sample.vtt", "sample.ass", "sample.ssa"])
def test_format_fixtures_parse_to_same_cues(name: str) -> None:
    cues = parse_bytes(read(name))
    assert cues == SAMPLE_CUES
    for cue in cues:
        assert "<" not in cue.text
        assert "{\\" not in cue.text


def test_broken_srt_keeps_only_valid_cues() -> None:
    assert parse_bytes(read("broken.srt")) == [
        Cue(1000, 3000, "First valid cue."),
        Cue(12000, 14000, "Second valid cue."),
    ]


@pytest.mark.parametrize("path", sorted(SUBS.glob("*.srt")) + sorted(SUBS.glob("sample.*")))
def test_round_trip_keeps_times_and_text(path: Path) -> None:
    cues = parse_bytes(path.read_bytes(), path.name[:3])
    assert parse_bytes(write_srt(cues)) == cues


def test_write_srt_exact_output() -> None:
    cues = [Cue(62030, 64560, "Later\nline"), Cue(-5, 1500, "Héllo")]
    assert (
        write_srt(cues)
        == (
            "1\n00:00:00,000 --> 00:00:01,500\nHéllo\n\n"
            "2\n00:01:02,030 --> 00:01:04,560\nLater\nline\n\n"
        ).encode()
    )


@pytest.mark.parametrize("text", ["", "hello world", "1\nnot a time\ntext\n"])
def test_unrecognized_text_raises(text: str) -> None:
    with pytest.raises(SubtitleError):
        parse_text(text)


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def test_zip_prefers_filename_match() -> None:
    data = make_zip(
        {
            "Show.S01E02.720p.srt": b"big" * 100,
            "Show.S01E02.1080p.srt": b"small",
        }
    )
    assert pick_from_zip(data, "Show.S01E02.1080p.mkv", 1, 2) == (
        "Show.S01E02.1080p.srt",
        b"small",
    )


@pytest.mark.parametrize("episode_name", ["Show S01E02.srt", "show.1x02.srt"])
def test_zip_matches_episode(episode_name: str) -> None:
    data = make_zip(
        {
            "Show S01E01.srt": b"x" * 500,
            "Show S01E12.srt": b"x" * 400,
            episode_name: b"episode",
        }
    )
    assert pick_from_zip(data, "other.mkv", 1, 2) == (episode_name, b"episode")


def test_zip_falls_back_to_largest_subtitle() -> None:
    data = make_zip(
        {
            "a.srt": b"12",
            "b.SRT": b"12345",
            "c.vtt": b"123",
            "readme.nfo": b"x" * 1000,
            "__MACOSX/d.srt": b"x" * 2000,
        }
    )
    assert pick_from_zip(data) == ("b.SRT", b"12345")


def test_zip_without_subtitles_or_bad_zip_returns_none() -> None:
    assert pick_from_zip(make_zip({"readme.nfo": b"hi"})) is None
    assert pick_from_zip(b"junk") is None
