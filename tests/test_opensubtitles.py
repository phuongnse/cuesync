"""Tests for the OpenSubtitles source. HTTP is faked with httpx.MockTransport."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from cuesync import __version__
from cuesync.config import UserConfig
from cuesync.sources import Candidate, OpenSubtitlesSource, SourceError, SubtitleRequest
from cuesync.sources import opensubtitles as os_module

FIXTURES = Path(__file__).parent / "fixtures" / "opensubtitles"
API_KEY = "key-SECRET-123"
USERNAME = "someone-name"
PASSWORD = "pw-SECRET-456"
TOKEN = "fake-login-token-9f3a"
LINK = "https://dl.opensubtitles.test/download/abc123/file.srt"
SRT = b"1\n00:00:01,000 --> 00:00:02,500\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nBye\n"
HASH = "8E245D9679D31E12"

Handler = Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Api:
    """A fake API that records requests."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.path}" for r in self.requests]


def run_with(api: Api, action, **source_args):
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
            source = OpenSubtitlesSource(API_KEY, client, **source_args)
            return await action(source)

    return asyncio.run(main())


def movie_request(**overrides) -> SubtitleRequest:
    values = {
        "imdb_id": "tt1375666",
        "season": None,
        "episode": None,
        "languages": ("eng", "fre"),
        "video_hash": HASH,
    }
    return SubtitleRequest(**{**values, **overrides})


def candidate(**overrides) -> Candidate:
    values = {
        "source": "opensubtitles",
        "source_id": "1001",
        "language": "eng",
        "release_name": "Inception.2010",
        "hash_match": True,
    }
    return Candidate(**{**values, **overrides})


def download_handler(
    *,
    login: Callable[[httpx.Request], httpx.Response] | None = None,
    download: Callable[[httpx.Request], httpx.Response] | None = None,
    body: bytes = SRT,
) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return login(request) if login else httpx.Response(200, json=fixture("login.json"))
        if request.url.path.endswith("/download"):
            if download:
                return download(request)
            return httpx.Response(200, json=fixture("download.json"))
        if str(request.url) == LINK:
            return httpx.Response(200, content=body)
        return httpx.Response(404)

    return handler


@pytest.fixture(autouse=True)
def _fresh_tokens():
    os_module.clear_tokens()
    yield
    os_module.clear_tokens()


def test_movie_search_params_and_candidates():
    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    found = run_with(api, lambda source: source.search(movie_request()))

    (request,) = api.requests
    assert request.method == "GET"
    assert request.url.host == "api.opensubtitles.com"
    assert request.url.path == "/api/v1/subtitles"
    assert request.url.params["imdb_id"] == "1375666"
    assert request.url.params["languages"] == "en,fr"
    assert request.url.params["moviehash"] == HASH.lower()
    assert "parent_imdb_id" not in request.url.params
    assert [key for key, _ in request.url.params.multi_items()] == sorted(
        key for key, _ in request.url.params.multi_items()
    )
    assert request.headers["Api-Key"] == API_KEY
    assert request.headers["User-Agent"] == f"CueSync v{__version__}"
    assert request.headers["Accept"] == "application/json"

    # The German result is not requested, so it is dropped. Hash matches come first.
    assert [(c.source_id, c.language, c.hash_match) for c in found] == [
        ("1002", "fre", True),
        ("1001", "eng", False),
    ]
    assert found[1].release_name == "Inception.2010.1080p.BluRay.x264"
    assert {c.source for c in found} == {"opensubtitles"}


def test_search_without_hash_or_with_a_bad_hash_leaves_moviehash_out():
    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    run_with(api, lambda source: source.search(movie_request(video_hash=None)))
    run_with(api, lambda source: source.search(movie_request(video_hash="not-a-hash")))
    assert all("moviehash" not in r.url.params for r in api.requests)


def test_release_name_falls_back_to_the_file_name():
    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    found = run_with(api, lambda source: source.search(movie_request(languages=("ger",))))
    assert [(c.language, c.release_name) for c in found] == [("ger", "Inception.german.srt")]


def test_episode_search_uses_parent_imdb_id_season_and_episode():
    api = Api(lambda request: httpx.Response(200, json=fixture("search_episode.json")))
    found = run_with(
        api,
        lambda source: source.search(
            movie_request(imdb_id="tt0903747", season=1, episode=2, video_hash=None)
        ),
    )
    params = api.requests[0].url.params
    assert params["parent_imdb_id"] == "903747"
    assert params["season_number"] == "1"
    assert params["episode_number"] == "2"
    assert "imdb_id" not in params
    assert [c.source_id for c in found] == ["2001"]


def test_unknown_languages_are_skipped(caplog):
    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    with caplog.at_level(logging.DEBUG):
        run_with(api, lambda source: source.search(movie_request(languages=("eng", "xxx"))))
    assert api.requests[0].url.params["languages"] == "en"
    assert "xxx" in caplog.text

    api = Api(lambda request: httpx.Response(200, json=fixture("search_movie.json")))
    assert run_with(api, lambda source: source.search(movie_request(languages=("xxx",)))) == []
    assert api.requests == []


def test_both_iso_forms_and_regional_codes_map():
    api = Api(lambda request: httpx.Response(200, json={"data": []}))
    run_with(api, lambda source: source.search(movie_request(languages=("fra", "pob", "zht"))))
    assert api.requests[0].url.params["languages"] == "fr,pt-br,zh-tw"


@pytest.mark.parametrize(("status", "word"), [(401, "401"), (429, "429"), (503, "503")])
def test_search_http_errors_give_no_candidates(status, word, caplog):
    api = Api(lambda request: httpx.Response(status, json={"message": "no"}))
    with caplog.at_level(logging.WARNING):
        found = run_with(api, lambda source: source.search(movie_request()))
    assert found == []
    assert word in caplog.text
    assert API_KEY not in caplog.text


def test_search_timeout_gives_no_candidates(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with caplog.at_level(logging.WARNING):
        found = run_with(Api(handler), lambda source: source.search(movie_request()))
    assert found == []
    assert "timed out" in caplog.text


def test_search_with_bad_json_gives_no_candidates(caplog):
    api = Api(lambda request: httpx.Response(200, content=b"<html>"))
    with caplog.at_level(logging.WARNING):
        assert run_with(api, lambda source: source.search(movie_request())) == []
    assert "unexpected answer" in caplog.text


def test_search_skips_malformed_entries():
    body = {
        "data": [
            "junk",
            {"attributes": {"language": "en"}},
            {"attributes": {"language": "en", "files": [{"file_id": 7}]}},
        ]
    }
    api = Api(lambda request: httpx.Response(200, json=body))
    found = run_with(api, lambda source: source.search(movie_request()))
    assert [c.source_id for c in found] == ["7"]


def test_download_without_login():
    api = Api(download_handler())
    subtitle = run_with(api, lambda source: source.download(candidate()))

    assert api.paths() == [
        "POST api.opensubtitles.com/api/v1/download",
        "GET dl.opensubtitles.test/download/abc123/file.srt",
    ]
    post, get = api.requests
    assert json.loads(post.content) == {"file_id": 1001}
    assert post.headers["Api-Key"] == API_KEY
    assert post.headers["User-Agent"] == f"CueSync v{__version__}"
    assert "Authorization" not in post.headers
    assert "Api-Key" not in get.headers
    assert "Authorization" not in get.headers

    assert subtitle.language == "eng"
    assert subtitle.source == "opensubtitles"
    assert subtitle.source_id == "1001"
    assert subtitle.release_name == "Inception.2010"
    assert subtitle.hash_match is True
    assert [(c.start_ms, c.end_ms, c.text) for c in subtitle.cues] == [
        (1000, 2500, "Hello there"),
        (3000, 4000, "Bye"),
    ]


def test_download_logs_in_and_reuses_the_token_across_sources():
    api = Api(download_handler())
    credentials = {"username": USERNAME, "password": PASSWORD}

    async def twice(source):
        await source.download(candidate())
        return await source.download(candidate(source_id="1002"))

    run_with(api, twice, **credentials)
    run_with(api, lambda source: source.download(candidate()), **credentials)

    logins = [r for r in api.requests if r.url.path.endswith("/login")]
    assert len(logins) == 1
    assert json.loads(logins[0].content) == {"username": USERNAME, "password": PASSWORD}
    assert logins[0].url.host == "api.opensubtitles.com"
    posts = [r for r in api.requests if r.url.path.endswith("/download")]
    assert len(posts) == 3
    for post in posts:
        assert post.url.host == "vip-api.opensubtitles.com"
        assert post.url.path == "/api/v1/download"
        assert post.headers["Authorization"] == f"Bearer {TOKEN}"
    assert [r.url.host for r in api.requests if str(r.url) == LINK] == ["dl.opensubtitles.test"] * 3
    assert all("Authorization" not in r.headers for r in api.requests if str(r.url) == LINK)


def test_expired_token_is_replaced(monkeypatch):
    api = Api(download_handler())
    now = [1000.0]
    monkeypatch.setattr(os_module.time, "monotonic", lambda: now[0])
    credentials = {"username": USERNAME, "password": PASSWORD}

    run_with(api, lambda source: source.download(candidate()), **credentials)
    now[0] += os_module.TOKEN_LIFETIME + 1
    run_with(api, lambda source: source.download(candidate()), **credentials)

    assert sum(r.url.path.endswith("/login") for r in api.requests) == 2


def test_failed_login_falls_back_to_anonymous_download(caplog):
    api = Api(download_handler(login=lambda request: httpx.Response(401, json={"message": "no"})))
    with caplog.at_level(logging.WARNING):
        subtitle = run_with(
            api,
            lambda source: source.download(candidate()),
            username=USERNAME,
            password=PASSWORD,
        )

    assert subtitle.cues
    post = next(r for r in api.requests if r.url.path.endswith("/download"))
    assert post.url.host == "api.opensubtitles.com"
    assert "Authorization" not in post.headers
    assert "login failed" in caplog.text
    assert PASSWORD not in caplog.text and USERNAME not in caplog.text


def test_unauthorized_download_drops_the_cached_token():
    api = Api(download_handler(download=lambda request: httpx.Response(401, json={})))
    credentials = {"username": USERNAME, "password": PASSWORD}
    with pytest.raises(SourceError, match="rejected"):
        run_with(api, lambda source: source.download(candidate()), **credentials)
    assert os_module._tokens == {}


def test_quota_error_raises_source_error_without_secrets(caplog):
    quota = httpx.Response(406, json={"message": "quota", "reset_time": "5 hours"})
    api = Api(download_handler(download=lambda request: quota))
    with caplog.at_level(logging.DEBUG), pytest.raises(SourceError) as info:
        run_with(
            api,
            lambda source: source.download(candidate()),
            username=USERNAME,
            password=PASSWORD,
        )

    assert "quota" in str(info.value)
    assert "5 hours" in str(info.value)
    for secret in (API_KEY, PASSWORD, TOKEN, LINK, "dl.opensubtitles.test"):
        assert secret not in caplog.text
        assert secret not in str(info.value)
    assert "quota" in caplog.text


@pytest.mark.parametrize(
    ("status", "word"), [(429, "rate limit"), (503, "HTTP 503"), (401, "rejected")]
)
def test_download_errors_raise_source_error(status, word):
    api = Api(download_handler(download=lambda request: httpx.Response(status, json={})))
    with pytest.raises(SourceError, match=word):
        run_with(api, lambda source: source.download(candidate()))


def test_download_timeout_raises_source_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(SourceError, match="timed out"):
        run_with(Api(handler), lambda source: source.download(candidate()))


def test_failing_link_raises_source_error_without_the_link(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/download"):
            return httpx.Response(200, json=fixture("download.json"))
        return httpx.Response(500)

    with caplog.at_level(logging.DEBUG), pytest.raises(SourceError) as info:
        run_with(Api(handler), lambda source: source.download(candidate()))
    assert "abc123" not in caplog.text
    assert "abc123" not in str(info.value)


def test_download_without_link_raises_source_error():
    api = Api(download_handler(download=lambda request: httpx.Response(200, json={})))
    with pytest.raises(SourceError):
        run_with(api, lambda source: source.download(candidate()))


def test_non_subtitle_body_raises_source_error():
    api = Api(download_handler(body=b"<html>not a subtitle</html>"))
    with pytest.raises(SourceError, match="not a usable subtitle"):
        run_with(api, lambda source: source.download(candidate()))


def test_bad_file_id_raises_source_error():
    api = Api(download_handler())
    with pytest.raises(SourceError):
        run_with(api, lambda source: source.download(candidate(source_id="abc")))
    assert api.requests == []


def test_from_config():
    client = httpx.AsyncClient()
    assert OpenSubtitlesSource.from_config(UserConfig(languages=["eng"]), client) is None
    config = UserConfig(
        languages=["eng"],
        opensubtitles_api_key=API_KEY,
        opensubtitles_username=USERNAME,
        opensubtitles_password=PASSWORD,
    )
    source = OpenSubtitlesSource.from_config(config, client)
    assert source is not None
    assert source.name == "opensubtitles"
