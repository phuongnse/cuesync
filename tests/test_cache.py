"""Tests for the SQLite and files cache."""

from __future__ import annotations

import logging

import numpy as np
import pytest

from cuesync.cache import FAILURE_RETRY_S, SEARCH_TTL_S, Cache
from cuesync.sources.base import Candidate, SubtitleRequest
from cuesync.subtitles import Cue
from cuesync.sync import Correction, Verdict

REQUEST = SubtitleRequest(
    imdb_id="tt1375666",
    season=None,
    episode=None,
    languages=("vie", "eng"),
    video_hash="abc",
    video_size=123,
    filename="a.mkv",
)
CANDIDATES = [
    Candidate("fake", "1", "vie", "Rel.One", hash_match=True, downloads=7),
    Candidate("fake", "2", "eng", "Rel.Two"),
]
CUES = (Cue(1000, 2500, "Xin chào"), Cue(3000, 4000, "Two\nlines"))
SYNCED = Correction(Verdict.SYNCED, 25 / 24, 2000, 5, 10.5)
FAILED = Correction(Verdict.FAILED, 1.0, 0, 1, 0.0)


@pytest.fixture
def now() -> list[float]:
    return [1000.0]


@pytest.fixture
def cache(tmp_path, now):
    cache = Cache(tmp_path, clock=lambda: now[0])
    yield cache
    cache.close()


def test_building_a_cache_touches_no_disk(tmp_path):
    Cache(tmp_path / "data")
    assert not (tmp_path / "data").exists()


def test_search_hit_and_miss(cache):
    assert cache.get_search("fake", REQUEST) is None
    cache.put_search("fake", REQUEST, CANDIDATES)
    assert cache.get_search("fake", REQUEST) == CANDIDATES
    assert cache.get_search("other", REQUEST) is None
    other = SubtitleRequest("tt1375666", None, None, ("vie", "eng"), "zzz", 123, "a.mkv")
    assert cache.get_search("fake", other) is None


def test_search_expires_after_the_ttl(cache, now):
    cache.put_search("fake", REQUEST, CANDIDATES)
    now[0] += SEARCH_TTL_S - 1
    assert cache.get_search("fake", REQUEST) == CANDIDATES
    now[0] += 1
    assert cache.get_search("fake", REQUEST) is None


def test_empty_search_results_are_not_stored(cache):
    cache.put_search("fake", REQUEST, [])
    assert cache.get_search("fake", REQUEST) is None


def test_subtitle_round_trip_and_persistence(tmp_path):
    first = Cache(tmp_path)
    assert first.get_subtitle("k") is None
    first.put_subtitle("k", CUES)
    assert first.get_subtitle("k") == CUES
    first.close()
    second = Cache(tmp_path)
    assert second.get_subtitle("k") == CUES
    second.close()


def test_subtitle_files_are_named_by_hash_not_by_key(cache, tmp_path):
    cache.put_subtitle("../../etc/passwd", CUES)
    names = [path.name for path in (tmp_path / "files").iterdir()]
    assert len(names) == 1
    assert names[0].endswith(".srt")
    assert "passwd" not in names[0]


def test_timeline_round_trip_and_persistence(tmp_path):
    windows = [
        (60.0, np.array([0, 1, 1, 0], dtype=np.float32)),
        (600.5, np.array([1, 0, 0, 0, 1], dtype=np.float32)),
    ]
    first = Cache(tmp_path)
    assert first.get_timeline("abc", 123) is None
    first.put_timeline("abc", 123, windows)
    first.close()
    second = Cache(tmp_path)
    stored = second.get_timeline("abc", 123)
    assert stored is not None
    assert [start for start, _ in stored] == [60.0, 600.5]
    for (_, expected), (_, actual) in zip(windows, stored, strict=True):
        np.testing.assert_array_equal(actual, expected)
        assert actual.dtype == expected.dtype
    assert second.get_timeline("abc", 124) is None
    second.close()


def test_empty_timeline_is_not_stored(cache):
    cache.put_timeline("abc", 123, [])
    assert cache.get_timeline("abc", 123) is None


def test_synced_result_is_kept_for_good(cache, now):
    assert cache.get_sync("abc", "k") is None
    cache.put_sync("abc", "k", SYNCED)
    now[0] += 365 * 24 * 3600
    assert cache.get_sync("abc", "k") == SYNCED
    assert cache.get_sync("abc", "other") is None
    assert cache.get_sync("zzz", "k") is None


def test_unchanged_result_is_kept_for_good(cache, now):
    unchanged = Correction(Verdict.UNCHANGED, 1.0, 0, 5, 1.0)
    cache.put_sync("abc", "k", unchanged)
    now[0] += 2 * FAILURE_RETRY_S
    assert cache.get_sync("abc", "k") == unchanged


def test_failed_result_is_returned_until_the_retry_time(cache, now):
    cache.put_sync("abc", "k", FAILED)
    now[0] += FAILURE_RETRY_S - 1
    assert cache.get_sync("abc", "k") == FAILED
    now[0] += 1
    assert cache.get_sync("abc", "k") is None


def test_evicts_the_least_recently_used_file_first(tmp_path):
    blob = Cue(0, 1000, "x" * 100)
    probe = Cache(tmp_path / "probe")
    probe.put_subtitle("a", [blob])
    size = (next((tmp_path / "probe" / "files").iterdir())).stat().st_size
    probe.close()
    cache = Cache(tmp_path / "data", max_bytes=2 * size + size // 2)
    cache.put_subtitle("a", [blob])
    cache.put_subtitle("b", [blob])
    assert cache.get_subtitle("a") is not None
    cache.put_subtitle("c", [blob])
    assert cache.get_subtitle("b") is None
    assert cache.get_subtitle("a") is not None
    assert cache.get_subtitle("c") is not None
    files = list((tmp_path / "data" / "files").iterdir())
    assert len(files) == 2
    assert sum(path.stat().st_size for path in files) <= 2 * size + size // 2
    cache.close()


def test_a_file_larger_than_the_limit_leaves_the_cache_within_it(tmp_path):
    cache = Cache(tmp_path, max_bytes=10)
    cache.put_subtitle("a", CUES)
    assert cache.get_subtitle("a") is None
    assert list((tmp_path / "files").iterdir()) == []
    cache.close()


def test_a_file_deleted_from_disk_is_a_miss(cache, tmp_path):
    cache.put_subtitle("k", CUES)
    for path in (tmp_path / "files").iterdir():
        path.unlink()
    assert cache.get_subtitle("k") is None
    cache.put_subtitle("k", CUES)
    assert cache.get_subtitle("k") == CUES


def test_a_broken_stored_file_is_a_miss_and_logged_by_type(cache, tmp_path, caplog):
    cache.put_subtitle("k", CUES)
    for path in (tmp_path / "files").iterdir():
        path.write_bytes(b"not a subtitle")
    with caplog.at_level(logging.WARNING):
        assert cache.get_subtitle("k") is None
    assert "SubtitleError" in caplog.text


def test_an_unusable_data_dir_never_raises(tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    cache = Cache(blocker / "data")
    cache.put_subtitle("k", CUES)
    cache.put_search("fake", REQUEST, CANDIDATES)
    cache.put_sync("abc", "k", SYNCED)
    assert cache.get_subtitle("k") is None
    assert cache.get_search("fake", REQUEST) is None
    assert cache.get_sync("abc", "k") is None
    assert "cache get_sync failed" in caplog.text
