"""Tests for the Torrentio stream lookup. HTTP is faked with httpx.MockTransport.

The fixtures under tests/fixtures/torrentio are real answers from torrentio.strem.fun for
`movie/tt1375666` and `series/tt0903747:1:2`, trimmed to six streams and without the tracker
`sources`. Real answers carry no `videoSize` and no season pack without `behaviorHints.filename`,
so three edits were made by hand to cover those shapes: a `videoSize` on the Tigole stream of
the movie, no `behaviorHints.filename` on the three season packs of the series, and no
`fileIdx` on the XEBEC stream of the series.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cuesync import __version__
from cuesync.stream import StreamRef, find_stream
from cuesync.stream import torrentio as torrentio_module
from cuesync.stremio import VideoId

FIXTURES = Path(__file__).parent / "fixtures" / "torrentio"
SECRET_URL = "https://torrentio.example/realdebrid=SECRET"
MOVIE = VideoId("tt1375666", None, None)
EPISODE = VideoId("tt0903747", 1, 2)

Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def answer(name: str) -> Handler:
    return lambda request: httpx.Response(200, json=fixture(name))


class Api:
    """A fake API that records requests."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def paths(self) -> list[str]:
        return [f"{r.url.host}{r.url.path}" for r in self.requests]


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    torrentio_module.clear_cache()
    monkeypatch.delenv("CUESYNC_TORRENTIO_URL", raising=False)
    yield
    torrentio_module.clear_cache()


def lookups(api: Api, *calls: dict) -> list[StreamRef | None]:
    """Run one find_stream call per dict of arguments on one client."""

    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
            return [await find_stream(client, **call) for call in calls]

    return asyncio.run(main())


def lookup(api: Api, **call) -> StreamRef | None:
    return lookups(api, call)[0]


def test_exact_filename_match():
    api = Api(answer("movie.json"))
    ref = lookup(
        api,
        video=MOVIE,
        filename="Inception.2010.4K.HDR.DV.2160p.BDRemux Ita Eng x265-NAHOM.mkv",
        size=None,
    )
    assert ref == StreamRef(
        info_hash="e10c8eb30c64876fb82a9a49e2b9186e42191fe2",
        file_idx=2,
        filename="Inception.2010.4K.HDR.DV.2160p.BDRemux Ita Eng x265-NAHOM.mkv",
        size=None,
    )
    assert api.paths() == ["torrentio.strem.fun/stream/movie/tt1375666.json"]
    assert api.requests[0].headers["User-Agent"] == f"CueSync v{__version__}"


def test_filename_match_ignores_case_and_folders():
    api = Api(answer("movie.json"))
    ref = lookup(
        api,
        video=MOVIE,
        filename="Downloads\\Movies/INCEPTION.2010.PROPER.BLURAY.2160P.AV1.HDR10.OPUS.5.1-UH.MKV",
        size=None,
    )
    assert ref is not None
    assert ref.info_hash == "2849211f150e966ef7924f43aec22727a644e10d"


def test_size_only_match():
    ref = lookup(Api(answer("movie.json")), video=MOVIE, filename=None, size=18500123456)
    assert ref == StreamRef(
        info_hash="c560112bc616685f3fac460ee3c848813c71489f",
        file_idx=0,
        filename="Inception (2010) (2160p x265 10bit HDR Tigole).mkv",
        size=18500123456,
    )


def test_size_match_is_the_fallback_for_an_unknown_filename():
    ref = lookup(Api(answer("movie.json")), video=MOVIE, filename="Other.mkv", size=18500123456)
    assert ref is not None
    assert ref.info_hash == "c560112bc616685f3fac460ee3c848813c71489f"


def test_size_without_a_match_gives_none():
    assert lookup(Api(answer("movie.json")), video=MOVIE, filename=None, size=123) is None


def test_a_different_known_size_rules_a_filename_match_out():
    ref = lookup(
        Api(answer("movie.json")),
        video=MOVIE,
        filename="Inception (2010) (2160p x265 10bit HDR Tigole).mkv",
        size=999,
    )
    assert ref is None


def test_a_stream_without_a_known_size_still_matches_a_filename():
    ref = lookup(
        Api(answer("movie.json")),
        video=MOVIE,
        filename="Inception.2010.PROPER.Bluray.2160p.AV1.HDR10.OPUS.5.1-UH.mkv",
        size=999,
    )
    assert ref is not None
    assert ref.size == 999


def test_two_torrents_with_the_same_filename_are_ambiguous():
    ref = lookup(
        Api(answer("movie.json")),
        video=MOVIE,
        filename="inception.2010.1080p.bluray.x265-rarbg.mp4",
        size=None,
    )
    assert ref is None


def test_no_filename_and_no_size_makes_no_request():
    api = Api(answer("movie.json"))
    assert lookup(api, video=MOVIE, filename=None, size=None) is None
    assert api.requests == []


def test_a_stream_without_file_idx_keeps_none():
    api = Api(answer("series.json"))
    ref = lookup(
        api,
        video=EPISODE,
        filename="Breaking Bad S01E02 The Cats in the Bag 2160p NF WEB-DL DDP5 1 H 265-XEBEC.mkv",
        size=None,
    )
    assert ref is not None
    assert ref.info_hash == "aa1a43787ff85fe5d029969857b0d64a6c9a05e4"
    assert ref.file_idx is None


def test_episode_filename_only_in_the_title():
    api = Api(answer("series.json"))
    name = "Breaking Bad (2008) - S01E02 - Cat's in the Bag... (1080p BluRay x265 Silence).mkv"
    ref = lookup(api, video=EPISODE, filename=name, size=None)
    assert ref == StreamRef(
        info_hash="b43df67a93863ea91f2f773f00361072da771dd3",
        file_idx=246,
        filename=name,
        size=None,
    )
    assert api.paths() == ["torrentio.strem.fun/stream/series/tt0903747:1:2.json"]


def test_episode_filename_in_the_title_before_a_flags_line():
    ref = lookup(
        Api(answer("series.json")),
        video=EPISODE,
        filename="Breaking.Bad.S01E02.2160p.WEBRip.DTS-HD.MA5.1.x264-TrollUHD.mkv",
        size=None,
    )
    assert ref is not None
    assert ref.info_hash == "1b42b99b9fbc0e0416a379787f250eb73b7434e3"
    assert ref.file_idx == 1


def test_malformed_streams_are_skipped():
    streams = [
        "text",
        {"infoHash": "not-a-hash", "behaviorHints": {"filename": "A.mkv"}},
        {"infoHash": "a" * 40, "fileIdx": True, "behaviorHints": {"filename": "A.mkv"}},
        {
            "infoHash": "B" * 40,
            "fileIdx": 3,
            "behaviorHints": {"filename": "B.mkv", "videoSize": True},
        },
        {"fileIdx": 1},
    ]
    api = Api(lambda request: httpx.Response(200, json={"streams": streams}))
    first, second = lookups(
        api,
        dict(video=MOVIE, filename="a.mkv", size=None),
        dict(video=MOVIE, filename="b.mkv", size=None),
    )
    assert first == StreamRef("a" * 40, None, "A.mkv", None)
    assert second == StreamRef("b" * 40, 3, "B.mkv", None)


def test_a_second_lookup_within_the_ttl_makes_no_request(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(torrentio_module.time, "monotonic", lambda: now[0])
    api = Api(answer("movie.json"))
    call = dict(video=MOVIE, filename="inception (2010) 1080p surround.mp4", size=None)
    other = dict(video=MOVIE, filename=None, size=18500123456)

    first, second = lookups(api, call, other)
    assert first is None  # the fixture has no such file, only the request count matters
    assert second is not None
    assert len(api.requests) == 1

    now[0] += torrentio_module.CACHE_TTL - 1
    lookups(api, call)
    assert len(api.requests) == 1

    now[0] += 1
    lookups(api, call)
    assert len(api.requests) == 2


def test_the_cache_is_per_title():
    api = Api(answer("movie.json"))
    lookups(
        api,
        dict(video=MOVIE, filename=None, size=1),
        dict(video=VideoId("tt0000001", None, None), filename=None, size=1),
    )
    assert api.paths() == [
        "torrentio.strem.fun/stream/movie/tt1375666.json",
        "torrentio.strem.fun/stream/movie/tt0000001.json",
    ]


def test_an_empty_answer_is_cached():
    api = Api(lambda request: httpx.Response(200, json={"streams": []}))
    call = dict(video=MOVIE, filename=None, size=1)
    assert lookups(api, call, call) == [None, None]
    assert len(api.requests) == 1


def test_base_url_from_the_environment(monkeypatch):
    monkeypatch.setenv("CUESYNC_TORRENTIO_URL", " https://torrentio.example/providers=yts/ ")
    api = Api(answer("movie.json"))
    lookup(api, video=MOVIE, filename=None, size=1)
    assert api.paths() == ["torrentio.example/providers=yts/stream/movie/tt1375666.json"]


def test_base_url_argument_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv("CUESYNC_TORRENTIO_URL", "https://env.example")
    api = Api(answer("movie.json"))
    lookup(api, video=MOVIE, filename=None, size=1, base_url="http://self.example:7000")
    assert api.requests[0].url.host == "self.example"
    assert api.requests[0].url.port == 7000


def test_a_blank_base_url_uses_the_default(monkeypatch):
    monkeypatch.setenv("CUESYNC_TORRENTIO_URL", "   ")
    api = Api(answer("movie.json"))
    lookup(api, video=MOVIE, filename=None, size=1)
    assert api.paths() == ["torrentio.strem.fun/stream/movie/tt1375666.json"]


def test_an_invalid_base_url_gives_none_without_a_request(monkeypatch, caplog):
    monkeypatch.setenv("CUESYNC_TORRENTIO_URL", "ftp://realdebrid=SECRET@torrentio.example")
    api = Api(answer("movie.json"))
    with caplog.at_level(logging.WARNING):
        assert lookup(api, video=MOVIE, filename=None, size=1) is None
    assert api.requests == []
    assert "SECRET" not in caplog.text
    assert "torrentio.example" not in caplog.text
    assert "base URL" in caplog.text


def raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(503),
        raise_timeout,
        lambda request: httpx.Response(200, text="<html>Just a moment...</html>"),
        lambda request: httpx.Response(200, json=["not", "a", "dict"]),
        lambda request: httpx.Response(200, json={"streams": "nope"}),
    ],
    ids=["http-503", "timeout", "html", "list", "streams-not-a-list"],
)
def test_failures_give_none_and_a_warning_without_the_base_url(handler, caplog):
    api = Api(handler)
    with caplog.at_level(logging.INFO):
        ref = lookup(api, video=MOVIE, filename="a.mkv", size=1, base_url=SECRET_URL)
    assert ref is None
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "SECRET" not in caplog.text
    assert "torrentio.example" not in caplog.text
    assert "realdebrid" not in caplog.text


def test_a_failure_is_not_cached():
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(503)
        return httpx.Response(200, json=fixture("movie.json"))

    api = Api(handler)
    call = dict(video=MOVIE, filename=None, size=18500123456)
    assert lookups(api, call) == [None]
    state["fail"] = False
    ref = lookups(api, call)[0]
    assert ref is not None
    assert len(api.requests) == 2
