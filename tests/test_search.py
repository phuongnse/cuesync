"""Tests for concurrent search, ranking and subtitle keys. Sources are fakes."""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field

import pytest

from cuesync.search import (
    decode_key,
    encode_key,
    gather_candidates,
    order_by_fit,
    rank,
    similarity,
)
from cuesync.sources import Candidate, SubtitleRequest
from cuesync.subtitles import Subtitle
from cuesync.sync import Correction, Verdict

REQUEST = SubtitleRequest(
    imdb_id="tt1375666", season=None, episode=None, languages=("eng",), filename="a.mkv"
)


def cand(
    source_id: str,
    language: str = "eng",
    release: str = "",
    hash_match: bool = False,
    downloads: int = 0,
    source: str = "fake",
) -> Candidate:
    return Candidate(source, source_id, language, release, hash_match, downloads)


@dataclass
class FakeSource:
    name: str
    candidates: list[Candidate] = field(default_factory=list)
    delay: float = 0.0
    error: Exception | None = None

    async def search(self, request: SubtitleRequest) -> list[Candidate]:
        await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return list(self.candidates)

    async def download(self, candidate: Candidate) -> Subtitle:
        raise NotImplementedError


def ids(candidates: list[Candidate]) -> list[str]:
    return [c.source_id for c in candidates]


def test_rank_hash_then_similarity_then_downloads():
    filename = "Inception.2010.1080p.BluRay.x264-GRP.mkv"
    candidates = [
        cand("popular", release="Something.Else.720p", downloads=9000),
        cand("close", release="Inception.2010.1080p.BluRay.x264-GRP", downloads=10),
        cand("hash", release="Unrelated", hash_match=True, downloads=1),
        cand("less-close", release="Inception.2010.720p", downloads=500),
    ]
    assert ids(rank(candidates, ["eng"], filename, 10)) == [
        "hash",
        "close",
        "less-close",
        "popular",
    ]


def test_rank_uses_downloads_when_similarity_ties_and_keeps_source_order_otherwise():
    candidates = [cand("a"), cand("b", downloads=5), cand("c"), cand("d", downloads=5)]
    assert ids(rank(candidates, ["eng"], None, 10)) == ["b", "d", "a", "c"]


def test_rank_orders_languages_by_preference_and_drops_others():
    candidates = [
        cand("1", "eng"),
        cand("2", "ger"),
        cand("3", "vie"),
        cand("4", "eng"),
    ]
    ranked = rank(candidates, ["vie", "eng"], None, 10)
    assert [(c.source_id, c.language) for c in ranked] == [
        ("3", "vie"),
        ("1", "eng"),
        ("4", "eng"),
    ]


def test_rank_limits_each_language():
    candidates = [cand(str(i), "eng") for i in range(8)] + [cand("v", "vie")]
    ranked = rank(candidates, ["eng", "vie"], None, 3)
    assert ids(ranked) == ["0", "1", "2", "v"]


def test_similarity():
    assert similarity("Movie.2010.1080p.mkv", "Movie.2010.1080p") == 1.0
    assert similarity("Movie.2010.1080p.mkv", "movie 2010 1080p.srt") == 1.0
    assert similarity("a.b.c", "a.b.d") == pytest.approx(0.5)
    assert similarity("Movie.mkv", "Other") == 0.0
    assert similarity(None, "Movie") == 0.0
    assert similarity("Movie.mkv", "") == 0.0
    assert similarity("", "Movie") == 0.0
    assert similarity("Movie.2010.1080p.BluRay", "Movie.2010.720p.WEB") < similarity(
        "Movie.2010.1080p.BluRay", "Movie.2010.1080p.WEB"
    )


def test_gather_merges_in_source_order_and_removes_duplicates():
    first = FakeSource("one", [cand("1", source="one"), cand("2", source="one")])
    second = FakeSource("two", [cand("2", source="two"), cand("1", source="one")])
    found = asyncio.run(gather_candidates([first, second], REQUEST))
    assert [(c.source, c.source_id) for c in found] == [("one", "1"), ("one", "2"), ("two", "2")]


def test_gather_without_sources_is_empty():
    assert asyncio.run(gather_candidates([], REQUEST)) == []


def test_slow_source_does_not_block_the_others(caplog):
    slow = FakeSource("slow", [cand("s", source="slow")], delay=10)
    fast = FakeSource("fast", [cand("f", source="fast")])

    started = time.monotonic()
    found = asyncio.run(gather_candidates([slow, fast], REQUEST, budget=0.2))
    elapsed = time.monotonic() - started

    assert ids(found) == ["f"]
    assert elapsed < 1
    assert "slow" in caplog.text


def test_failing_source_is_skipped(caplog):
    broken = FakeSource("broken", error=RuntimeError("boom"))
    fine = FakeSource("fine", [cand("f", source="fine")])
    found = asyncio.run(gather_candidates([broken, fine], REQUEST))
    assert ids(found) == ["f"]
    assert "broken" in caplog.text


def test_key_round_trip():
    candidate = cand("123", "vie", source="subdl")
    key = encode_key(candidate)
    assert decode_key(key) == ("subdl", "123", "vie")
    assert "=" not in key
    assert all(ch.isalnum() or ch in "-_" for ch in key)


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "key",
    [
        "",
        "!!!",
        "not-base64-json",
        b64("not json"),
        b64('{"a":1}'),
        b64('["a","b"]'),
        b64('["a","b","c","d"]'),
        b64('["a",1,"c"]'),
        b64('["a",null,"c"]'),
        "é",
    ],
)
def test_decode_key_rejects_garbage(key):
    with pytest.raises(ValueError):
        decode_key(key)


def test_order_by_fit_puts_measured_first_by_confidence_then_unmeasured_then_failed():
    subs = [Candidate("fake", str(i), "vie", "") for i in range(1, 7)]
    results = {
        encode_key(subs[0]): Correction(Verdict.FAILED, 1.0, 0, 0, 0.0),
        encode_key(subs[1]): Correction(Verdict.SYNCED, 1.0, 900, 5, 1.0, 6.0),
        encode_key(subs[3]): Correction(Verdict.UNCHANGED, 1.0, 0, 5, 1.0, 8.0),
        encode_key(subs[5]): Correction(Verdict.SYNCED, 1.0, 900, 5, 1.0, 5.0),
    }
    ordered = order_by_fit(subs, results)
    assert [c.source_id for c in ordered] == ["4", "2", "6", "3", "5", "1"]
    assert order_by_fit(subs, {}) == subs
