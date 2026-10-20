"""Tests for the SubDL source. HTTP is faked with httpx.MockTransport."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cuesync import __version__
from cuesync.config import UserConfig
from cuesync.sources import Candidate, SourceError, SubDLSource, SubtitleRequest

FIXTURES = Path(__file__).parent / "fixtures" / "subdl"
API_KEY = "subdl-SECRET-123"
PACK = (FIXTURES / "pack.zip").read_bytes()
SRT = b"1\n00:00:01,000 --> 00:00:02,500\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n"
EPISODE_LINK = "/subtitle/4100001-4200001.zip"
MOVIE_LINK = "/subtitle/3197651-3213944.zip"

Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buffer.getvalue()


class Api:
    """A fake API that records requests."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def run_with(api: Api, action):
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
            return await action(SubDLSource(API_KEY, client))

    return asyncio.run(main())


def movie_request(**overrides) -> SubtitleRequest:
    values = {
        "imdb_id": "tt1375666",
        "season": None,
        "episode": None,
        "languages": ("eng", "vie"),
    }
    return SubtitleRequest(**{**values, **overrides})


def episode_request(**overrides) -> SubtitleRequest:
    values = {"imdb_id": "tt0903747", "season": 1, "episode": 2, "languages": ("eng", "vie")}
    return SubtitleRequest(**{**values, **overrides})


def candidate(**overrides) -> Candidate:
    values = {
        "source": "subdl",
        "source_id": f"{EPISODE_LINK}:1:2",
        "language": "eng",
        "release_name": "Breaking.Bad.S01.1080p.BluRay.x264",
    }
    return Candidate(**{**values, **overrides})


def search_answer(body: dict) -> Handler:
    return lambda request: httpx.Response(200, json=body)


def test_movie_search_params_and_candidates():
    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    found = run_with(api, lambda source: source.search(movie_request()))

    (request,) = api.requests
    assert request.method == "GET"
    assert request.url.host == "api.subdl.com"
    assert request.url.path == "/api/v1/subtitles"
    assert request.url.params["api_key"] == API_KEY
    assert request.url.params["imdb_id"] == "tt1375666"
    assert request.url.params["languages"] == "EN,VI"
    assert request.url.params["type"] == "movie"
    assert "season_number" not in request.url.params
    assert "episode_number" not in request.url.params
    keys = [key for key, _ in request.url.params.multi_items()]
    assert keys == sorted(keys)

    # The German entry is not requested and the bad link is dropped. The entry with no
    # release name falls back to its name.
    assert [(c.source_id, c.language, c.release_name) for c in found] == [
        (MOVIE_LINK, "eng", "Inception.2010.1080p.BluRay.x264"),
        ("/subtitle/3197652-3213945.zip", "vie", "Inception.2010.720p.BluRay.x264-SPARKS"),
        ("/subtitle/3197654-3213947.zip", "eng", "Inception 2010"),
    ]
    assert {c.source for c in found} == {"subdl"}
    assert not any(c.hash_match for c in found)


def test_episode_search_params_and_candidate_ids():
    api = Api(lambda request: httpx.Response(200, json=fixture("search_episode.json")))
    found = run_with(api, lambda source: source.search(episode_request()))

    (request,) = api.requests
    assert request.url.params["imdb_id"] == "tt0903747"
    assert request.url.params["type"] == "tv"
    assert request.url.params["season_number"] == "1"
    assert request.url.params["episode_number"] == "2"
    assert request.url.params["languages"] == "EN,VI"
    assert [c.source_id for c in found] == [
        f"{EPISODE_LINK}:1:2",
        "/subtitle/4100002-4200002.zip:1:2",
        "/subtitle/4100003-4200003.zip:1:2",
    ]
    assert [c.language for c in found] == ["eng", "eng", "vie"]
    assert found[1].release_name == "Breaking.Bad.S01E02.720p.WEB"


def test_unknown_languages_are_skipped(caplog):
    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    with caplog.at_level(logging.DEBUG):
        run_with(api, lambda source: source.search(movie_request(languages=("eng", "xxx"))))
    assert api.requests[0].url.params["languages"] == "EN"
    assert "xxx" in caplog.text

    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    assert run_with(api, lambda source: source.search(movie_request(languages=("xxx",)))) == []
    assert api.requests == []


def test_regional_codes_map_both_ways():
    body = {
        "status": True,
        "subtitles": [
            {"language": "BR_PT", "url": "/subtitle/1-1.zip", "release_name": "a"},
            {"language": "zh_bg", "url": "/subtitle/2-2.zip", "release_name": "b"},
        ],
    }
    api = Api(search_answer(body))
    found = run_with(
        api, lambda source: source.search(movie_request(languages=("fra", "pob", "zht")))
    )
    assert api.requests[0].url.params["languages"] == "BR_PT,FR,ZH_BG"
    assert [(c.language, c.source_id) for c in found] == [
        ("pob", "/subtitle/1-1.zip"),
        ("zht", "/subtitle/2-2.zip"),
    ]


@pytest.mark.parametrize("imdb_id", ["1375666", "tt", "tt12x"])
def test_non_imdb_id_sends_no_request(imdb_id):
    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    found = run_with(api, lambda source: source.search(movie_request(imdb_id=imdb_id)))
    assert found == []
    assert api.requests == []


@pytest.mark.parametrize(
    ("status", "text"), [(403, "rejected the API key"), (429, "rate limit"), (500, "HTTP 500")]
)
def test_search_http_errors_give_no_candidates(status, text, caplog):
    api = Api(lambda request: httpx.Response(status, json={"message": "nope"}))
    with caplog.at_level(logging.DEBUG):
        assert run_with(api, lambda source: source.search(movie_request())) == []
    assert text in caplog.text
    assert API_KEY not in caplog.text


def test_search_timeout_gives_no_candidates(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with caplog.at_level(logging.DEBUG):
        found = run_with(Api(handler), lambda source: source.search(movie_request()))
    assert found == []
    assert "timed out" in caplog.text
    assert API_KEY not in caplog.text


def test_search_with_bad_json_gives_no_candidates(caplog):
    api = Api(lambda request: httpx.Response(200, content=b"<html>"))
    with caplog.at_level(logging.DEBUG):
        assert run_with(api, lambda source: source.search(movie_request())) == []
    assert "unexpected answer" in caplog.text
    assert API_KEY not in caplog.text


def test_status_false_gives_no_candidates_and_a_warning_without_server_text(caplog):
    api = Api(search_answer({"status": False, "error": "Invalid api key SUBDL-LEAK"}))
    with caplog.at_level(logging.DEBUG):
        assert run_with(api, lambda source: source.search(movie_request())) == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.getMessage() for r in warnings] == ["SubDL search failed: SubDL reported an error"]
    assert "SUBDL-LEAK" not in caplog.text
    assert API_KEY not in caplog.text


def test_title_not_found_is_logged_at_debug_only(caplog):
    api = Api(search_answer({"status": False, "error": "Can't find movie with that IMDb id"}))
    with caplog.at_level(logging.DEBUG):
        assert run_with(api, lambda source: source.search(movie_request())) == []
    assert "Can't find movie" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_search_skips_malformed_entries():
    body = {
        "status": True,
        "subtitles": [
            "junk",
            [],
            {"language": "EN"},
            {"language": None, "url": "/subtitle/1-1.zip"},
            {"language": "EN", "url": 5},
            {"language": "EN", "url": "https://evil.example/x.zip"},
            {"language": "EN", "url": "/subtitle/7-8.zip"},
        ],
    }
    found = run_with(Api(search_answer(body)), lambda source: source.search(movie_request()))
    assert [c.source_id for c in found] == ["/subtitle/7-8.zip"]


def test_search_drops_the_query_string_and_key_from_links():
    body = {
        "status": True,
        "subtitles": [{"language": "EN", "url": f"/subtitle/7-8.zip?api_key={API_KEY}"}],
    }
    found = run_with(Api(search_answer(body)), lambda source: source.search(episode_request()))
    assert [c.source_id for c in found] == ["/subtitle/7-8.zip:1:2"]


def test_search_ignores_a_subtitles_field_that_is_not_a_list():
    body = {"status": True, "subtitles": "nope"}
    assert run_with(Api(search_answer(body)), lambda source: source.search(movie_request())) == []


def test_duplicate_links_are_kept_once():
    body = {
        "status": True,
        "subtitles": [
            {"language": "EN", "url": "/subtitle/7-8.zip", "release_name": "first"},
            {"language": "EN", "url": "/subtitle/7-8.zip", "release_name": "second"},
        ],
    }
    found = run_with(Api(search_answer(body)), lambda source: source.search(episode_request()))
    assert [(c.source_id, c.release_name) for c in found] == [("/subtitle/7-8.zip:1:2", "first")]


def test_episode_download_picks_the_requested_episode_from_a_season_pack():
    api = Api(lambda request: httpx.Response(200, content=PACK))
    subtitle = run_with(api, lambda source: source.download(candidate()))

    (request,) = api.requests
    assert request.url.host == "dl.subdl.com"
    assert request.url.path == EPISODE_LINK
    assert request.headers["User-Agent"] == f"CueSync v{__version__}"
    assert API_KEY not in str(request.url)

    assert [(c.start_ms, c.end_ms, c.text) for c in subtitle.cues] == [
        (3000, 4500, "Second episode line"),
    ]
    assert subtitle.language == "eng"
    assert subtitle.source == "subdl"
    assert subtitle.source_id == f"{EPISODE_LINK}:1:2"
    assert subtitle.release_name == "Breaking.Bad.S01.1080p.BluRay.x264"


def test_movie_download_returns_the_subtitle_in_the_zip():
    api = Api(lambda request: httpx.Response(200, content=make_zip({"Inception.srt": SRT})))
    subtitle = run_with(
        api,
        lambda source: source.download(
            candidate(source_id=MOVIE_LINK, language="vie", release_name="Inception")
        ),
    )
    assert api.requests[0].url.path == MOVIE_LINK
    assert [(c.start_ms, c.end_ms, c.text) for c in subtitle.cues] == [
        (1000, 2500, "Hello there"),
        (3000, 4000, "Bye"),
    ]
    assert subtitle.language == "vie"
    assert subtitle.release_name == "Inception"


def test_zip_without_subtitles_raises_source_error(caplog):
    zipped = make_zip({"readme.txt": b"hello", "movie.nfo": b"info"})
    api = Api(lambda request: httpx.Response(200, content=zipped))
    with caplog.at_level(logging.DEBUG), pytest.raises(SourceError, match="no usable subtitle"):
        run_with(api, lambda source: source.download(candidate()))
    assert API_KEY not in caplog.text


def test_non_zip_download_raises_source_error():
    api = Api(lambda request: httpx.Response(200, content=b"<html>"))
    with pytest.raises(SourceError, match="no usable subtitle"):
        run_with(api, lambda source: source.download(candidate()))


def test_unusable_subtitle_file_raises_source_error():
    zipped = make_zip({"movie.srt": b"<html>not a subtitle</html>"})
    api = Api(lambda request: httpx.Response(200, content=zipped))
    with pytest.raises(SourceError, match="not a usable subtitle"):
        run_with(api, lambda source: source.download(candidate(source_id=MOVIE_LINK)))


@pytest.mark.parametrize(
    ("status", "text"), [(403, "rejected the API key"), (404, "HTTP 404"), (429, "rate limit")]
)
def test_download_http_errors_raise_source_error_without_the_key(status, text, caplog):
    api = Api(lambda request: httpx.Response(status, json={}))
    with caplog.at_level(logging.DEBUG), pytest.raises(SourceError) as info:
        run_with(api, lambda source: source.download(candidate()))
    assert text in str(info.value)
    assert API_KEY not in str(info.value)
    assert API_KEY not in caplog.text


def test_download_timeout_raises_source_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(SourceError, match="timed out"):
        run_with(Api(handler), lambda source: source.download(candidate()))


@pytest.mark.parametrize(
    "source_id",
    [
        "https://evil.example/subtitle/1-1.zip",
        "/subtitle/../secret.zip",
        "/subtitle/1-1.zip:1",
        "/subtitle/1-1.zip:x:2",
        "/subtitle/1-1.zip\n",
    ],
)
def test_bad_source_id_raises_before_any_request(source_id):
    api = Api(lambda request: httpx.Response(200, content=PACK))
    with pytest.raises(SourceError, match="not valid") as info:
        run_with(api, lambda source: source.download(candidate(source_id=source_id)))
    assert api.requests == []
    assert API_KEY not in str(info.value)


def test_from_config():
    client = httpx.AsyncClient()
    assert SubDLSource.from_config(UserConfig(languages=["eng"]), client) is None
    source = SubDLSource.from_config(UserConfig(languages=["eng"], subdl_api_key=API_KEY), client)
    assert source is not None
    assert source.name == "subdl"
