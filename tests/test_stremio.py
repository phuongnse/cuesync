import pytest

from cuesync.stremio import Extra, VideoId, parse_extra, parse_video_id, raw_extra_segment


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [
        (b"/subtitles/movie/tt1/videoHash=a.json", "videoHash=a"),
        (b"/cfg/subtitles/movie/tt1/filename=a%2Fb.mkv.json", "filename=a%2Fb.mkv"),
        (b"/subtitles/movie/tt1/filename=a%26b.json", "filename=a%26b"),
        (b"/subtitles/movie/tt1/filename=%C3%A9.json", "filename=%C3%A9"),
        (b"/subtitles/movie/tt1/filename=\xff.json", "filename=\ufffd"),
        (b"/subtitles/movie/tt1/videoHash=a", "videoHash=a"),
    ],
)
def test_raw_extra_segment_keeps_escapes(raw_path, expected):
    assert raw_extra_segment(raw_path) == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a%26b.mkv", "a&b.mkv"),
        ("a%3Db.mkv", "a=b.mkv"),
        ("a%2Fb.mkv", "a/b.mkv"),
        ("a%20b.mkv", "a b.mkv"),
        ("a+b.mkv", "a b.mkv"),
        ("a%2Bb.mkv", "a+b.mkv"),
        ("caf%C3%A9.mkv", "café.mkv"),
        ("a%26b%3Dc%2Fd%20e%2Bf%C3%A9.mkv", "a&b=c/d e+fé.mkv"),
    ],
)
def test_parse_extra_decodes_filename(filename, expected):
    assert parse_extra(f"filename={filename}").filename == expected


def test_parse_extra_reads_all_fields():
    assert parse_extra("videoHash=abc&videoSize=123&filename=a%20b.mkv") == Extra(
        video_hash="abc", video_size=123, filename="a b.mkv"
    )


def test_parse_extra_ignores_unknown_keys():
    assert parse_extra("foo=bar&videoHash=abc") == Extra("abc", None, None)


@pytest.mark.parametrize("size", ["abc", "-5", "", "+12", "1_000", "%201", "1.5", "\u0661\u0662"])
def test_parse_extra_invalid_size_is_none(size):
    assert parse_extra(f"videoSize={size}").video_size is None


def test_parse_extra_empty_values_are_none():
    assert parse_extra("videoHash=&filename=") == Extra(None, None, None)
    assert parse_extra("") == Extra(None, None, None)


def test_parse_video_id_movie():
    assert parse_video_id("tt1375666") == VideoId("tt1375666", None, None)


def test_parse_video_id_episode():
    assert parse_video_id("tt0903747:1:2") == VideoId("tt0903747", 1, 2)


@pytest.mark.parametrize(
    "id", ["", "abc", "tt", "tt123:1", "tt1:a:b", "tt1:1:2:3", "xx1", "tt1:1:2\n", " tt1"]
)
def test_parse_video_id_rejects_malformed(id):
    with pytest.raises(ValueError):
        parse_video_id(id)
