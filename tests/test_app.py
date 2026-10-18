import base64
import json
import logging
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from cuesync import __version__
from cuesync import app as app_module
from cuesync.app import create_app
from cuesync.config import UserConfig, encode_config
from cuesync.search import decode_key, encode_key
from cuesync.sources import Candidate, SourceError, SubtitleRequest
from cuesync.stremio import Extra, parse_extra
from cuesync.subtitles import Cue, Subtitle


def no_sources(config, client):
    return []


client = TestClient(create_app(source_factory=no_sources))

API_KEY = "super-secret-subdl-key"
TOKEN = encode_config(UserConfig(languages=["vie", "eng"], subdl_api_key=API_KEY))
BAD_TOKEN = "bad-" + TOKEN


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_bare_manifest_requires_configuration():
    response = client.get("/manifest.json")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["id"] == "community.cuesync"
    assert manifest["version"] == __version__
    assert manifest["resources"] == ["subtitles"]
    assert manifest["idPrefixes"] == ["tt"]
    assert manifest["behaviorHints"] == {"configurable": True, "configurationRequired": True}


def test_configured_manifest_is_configurable():
    response = client.get(f"/{TOKEN}/manifest.json")
    assert response.status_code == 200
    assert response.json()["behaviorHints"] == {"configurable": True}


def test_manifest_allows_cross_origin_requests():
    response = client.get("/manifest.json", headers={"Origin": "https://web.stremio.com"})
    assert response.headers["access-control-allow-origin"] == "*"


def test_subtitles_without_extra():
    response = client.get(f"/{TOKEN}/subtitles/movie/tt1375666.json")
    assert response.json() == {"subtitles": []}


def test_subtitles_with_extra():
    response = client.get(
        f"/{TOKEN}/subtitles/series/tt0903747:1:2/"
        "videoHash=abc&videoSize=123&filename=a%20b.mkv.json"
    )
    assert response.status_code == 200
    assert response.json() == {"subtitles": []}


def test_unprefixed_subtitles_are_gone():
    assert client.get("/subtitles/movie/tt1375666.json").status_code == 404


def test_invalid_config_returns_400_without_echoing_it():
    for path in ("manifest.json", "subtitles/movie/tt1375666.json"):
        response = client.get(f"/{BAD_TOKEN}/{path}")
        assert response.status_code == 400
        assert "/configure" in response.json()["detail"]
        assert BAD_TOKEN not in response.text
        assert API_KEY not in response.text


def test_configure_pages_are_self_contained_html():
    for path in ("/configure", f"/{TOKEN}/configure"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert 'name="viewport"' in response.text
        assert "<script src" not in response.text
        for name in UserConfig.model_fields:
            assert f'name="{name}"' in response.text


def test_access_log_hides_config(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    # The test client's own httpx logger prints the full URL. A real server never does.
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", True)
    test_client = TestClient(create_app())
    assert test_client.get(f"/{TOKEN}/manifest.json").status_code == 200
    assert test_client.get(f"/{BAD_TOKEN}/manifest.json").status_code == 400
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", f"/{TOKEN}/manifest.json", "1.1", 200
    )
    assert "<config>" in caplog.text
    assert API_KEY not in caplog.text
    assert TOKEN not in caplog.text


def test_access_log_filter_is_attached_once():
    create_app()
    create_app()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(type(f).__name__ == "RedactConfigFilter" for f in filters) == 1


@pytest.fixture
def parsed(monkeypatch):
    """Record what the subtitles route parsed from `extra`."""
    results: list[Extra] = []

    def recording_parse_extra(extra: str) -> Extra:
        result = parse_extra(extra)
        results.append(result)
        return result

    monkeypatch.setattr(app_module, "parse_extra", recording_parse_extra)
    return results


def test_subtitles_with_encoded_filename(parsed):
    response = client.get(
        f"/{TOKEN}/subtitles/movie/tt1375666/"
        "videoHash=abc&videoSize=123&filename=a%26b%3Dc%2Fd%20e%2Bf%C3%A9.mkv.json"
    )
    assert response.status_code == 200
    assert response.json() == {"subtitles": []}
    assert parsed == [Extra(video_hash="abc", video_size=123, filename="a&b=c/d e+fé.mkv")]


def test_subtitles_for_episode_with_extra(parsed):
    response = client.get(
        f"/{TOKEN}/subtitles/series/tt0903747:1:2/videoHash=x&videoSize=9&filename=s01e02.mkv.json"
    )
    assert response.status_code == 200
    assert parsed == [Extra(video_hash="x", video_size=9, filename="s01e02.mkv")]


@pytest.mark.parametrize("id", ["abc", "tt123:1", "tt1:a:b", "tt1:1:2:3"])
def test_subtitles_reject_malformed_id(id):
    assert client.get(f"/{TOKEN}/subtitles/series/{id}.json").status_code == 400


def test_subtitles_with_extra_reject_malformed_id():
    assert client.get(f"/{TOKEN}/subtitles/series/tt0903747:1/videoHash=x.json").status_code == 400


PUBLIC_URL = "https://cuesync.example.ts.net"


def test_configure_uses_public_url_argument():
    response = TestClient(create_app(public_url=PUBLIC_URL + "/")).get("/configure")
    assert f'data-base-url="{PUBLIC_URL}"' in response.text


def test_configure_uses_public_url_from_environment(monkeypatch):
    monkeypatch.setenv("CUESYNC_PUBLIC_URL", f" {PUBLIC_URL}/ ")
    response = TestClient(create_app()).get(f"/{TOKEN}/configure")
    assert f'data-base-url="{PUBLIC_URL}"' in response.text


@pytest.mark.parametrize("value", [None, ""])
def test_configure_uses_request_url_without_public_url(monkeypatch, value):
    monkeypatch.delenv("CUESYNC_PUBLIC_URL", raising=False)
    for path in ("/configure", f"/{TOKEN}/configure"):
        response = TestClient(create_app(public_url=value)).get(path)
        assert 'data-base-url="http://testserver"' in response.text


def test_empty_environment_value_counts_as_unset(monkeypatch):
    monkeypatch.setenv("CUESYNC_PUBLIC_URL", "  ")
    response = TestClient(create_app()).get("/configure")
    assert 'data-base-url="http://testserver"' in response.text


@pytest.mark.parametrize("value", ["cuesync.example.ts.net", "ftp://x"])
def test_public_url_without_http_scheme_is_rejected(value):
    with pytest.raises(ValueError, match="CUESYNC_PUBLIC_URL"):
        create_app(public_url=value)


def test_configure_page_has_no_placeholder_or_location_origin():
    text = TestClient(create_app(public_url=PUBLIC_URL)).get("/configure").text
    assert "{{BASE_URL}}" not in text
    assert "location.origin" not in text
    assert "location.host" not in text


@dataclass
class FakeSource:
    name: str = "fake"
    candidates: list[Candidate] = field(default_factory=list)
    cues: tuple[Cue, ...] = (Cue(1000, 2500, "Hello there"),)
    error: str | None = None
    searches: list[SubtitleRequest] = field(default_factory=list)
    downloads: list[Candidate] = field(default_factory=list)

    async def search(self, request: SubtitleRequest) -> list[Candidate]:
        self.searches.append(request)
        return list(self.candidates)

    async def download(self, candidate: Candidate) -> Subtitle:
        self.downloads.append(candidate)
        if self.error is not None:
            raise SourceError(self.error)
        return Subtitle(
            cues=self.cues,
            language=candidate.language,
            source=self.name,
            source_id=candidate.source_id,
            release_name=candidate.release_name,
        )


def fake_client(source: FakeSource) -> TestClient:
    return TestClient(create_app(source_factory=lambda config, http: [source]))


def fake_candidate(source_id: str, language: str = "eng", **kwargs) -> Candidate:
    return Candidate("fake", source_id, language, kwargs.pop("release_name", ""), **kwargs)


def test_subtitles_list_entries_link_to_srt_files():
    source = FakeSource(
        candidates=[fake_candidate("1", "eng"), fake_candidate("2", "vie", hash_match=True)]
    )
    response = fake_client(source).get(
        f"/{TOKEN}/subtitles/movie/tt1375666/videoHash=abc&filename=a.mkv.json"
    )
    assert response.status_code == 200
    entries = response.json()["subtitles"]
    # The user's language order decides the order of the list.
    assert [entry["lang"] for entry in entries] == ["vie", "eng"]
    for entry in entries:
        assert set(entry) == {"id", "url", "lang"}
        assert entry["url"] == f"http://testserver/{TOKEN}/sub/{entry['id']}.srt"
    assert decode_key(entries[0]["id"]) == ("fake", "2", "vie")
    (search,) = source.searches
    assert search.imdb_id == "tt1375666"
    assert search.languages == ("vie", "eng")
    assert search.video_hash == "abc"
    assert search.filename == "a.mkv"
    assert source.downloads == []


def test_subtitles_without_extra_search_with_the_id_only():
    source = FakeSource(candidates=[fake_candidate("1")])
    response = fake_client(source).get(f"/{TOKEN}/subtitles/series/tt0903747:1:2.json")
    assert len(response.json()["subtitles"]) == 1
    (search,) = source.searches
    assert (search.season, search.episode) == (1, 2)
    assert (search.video_hash, search.video_size, search.filename) == (None, None, None)


def test_subtitles_urls_use_the_public_url():
    source = FakeSource(candidates=[fake_candidate("1")])
    test_client = TestClient(
        create_app(public_url=PUBLIC_URL, source_factory=lambda config, http: [source])
    )
    (entry,) = test_client.get(f"/{TOKEN}/subtitles/movie/tt1375666.json").json()["subtitles"]
    assert entry["url"] == f"{PUBLIC_URL}/{TOKEN}/sub/{entry['id']}.srt"


def test_subtitles_list_is_limited_per_language():
    source = FakeSource(candidates=[fake_candidate(str(i)) for i in range(8)])
    token = encode_config(UserConfig(languages=["eng"], max_per_language=3))
    response = fake_client(source).get(f"/{token}/subtitles/movie/tt1375666.json")
    assert len(response.json()["subtitles"]) == 3


def test_subtitles_list_defaults_to_five_per_language():
    source = FakeSource(candidates=[fake_candidate(str(i)) for i in range(8)])
    response = fake_client(source).get(f"/{TOKEN}/subtitles/movie/tt1375666.json")
    assert len(response.json()["subtitles"]) == 5


def test_subtitle_keys_hold_no_secrets():
    source = FakeSource(candidates=[fake_candidate("1")])
    token = encode_config(
        UserConfig(languages=["eng"], opensubtitles_api_key=API_KEY, subdl_api_key=API_KEY)
    )
    (entry,) = (
        fake_client(source).get(f"/{token}/subtitles/movie/tt1375666.json").json()["subtitles"]
    )
    padded = entry["id"] + "=" * (-len(entry["id"]) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded)) == ["fake", "1", "eng"]
    assert API_KEY not in entry["id"]


def srt_path(source_id: str = "7", language: str = "eng", source: str = "fake") -> str:
    return f"/{TOKEN}/sub/{encode_key(Candidate(source, source_id, language, ''))}.srt"


def test_srt_endpoint_serves_utf8_srt():
    source = FakeSource(cues=(Cue(1000, 2500, "Xin chào"),))
    response = fake_client(source).get(srt_path("7", "vie"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-subrip; charset=utf-8"
    assert response.content.decode("utf-8") == "1\n00:00:01,000 --> 00:00:02,500\nXin chào\n\n"
    (download,) = source.downloads
    assert (download.source, download.source_id, download.language) == ("fake", "7", "vie")


def test_srt_endpoint_serves_repeat_fetches_from_the_cache():
    source = FakeSource()
    test_client = fake_client(source)
    first = test_client.get(srt_path())
    second = test_client.get(srt_path())
    assert first.content == second.content
    assert len(source.downloads) == 1


def test_srt_link_is_served_by_a_fresh_app_without_a_download():
    source = FakeSource(cues=(Cue(1000, 2500, "Xin chào"),))
    first = fake_client(source).get(srt_path("7", "vie"))
    other = FakeSource()
    second = fake_client(other).get(srt_path("7", "vie"))
    assert second.status_code == 200
    assert second.content == first.content
    assert len(source.downloads) == 1
    assert other.downloads == []


def test_two_subtitles_requests_search_once():
    source = FakeSource(candidates=[fake_candidate("1")])
    test_client = fake_client(source)
    path = f"/{TOKEN}/subtitles/movie/tt1375666/videoHash=abc&filename=a.mkv.json"
    first = test_client.get(path)
    second = test_client.get(path)
    assert first.json() == second.json()
    assert len(source.searches) == 1


def test_cache_lives_in_the_data_dir_from_the_environment(tmp_path, monkeypatch):
    data = tmp_path / "elsewhere"
    monkeypatch.setenv("CUESYNC_DATA_DIR", str(data))
    assert not data.exists()
    test_client = fake_client(FakeSource())
    assert not data.exists()
    test_client.get(srt_path())
    assert (data / "cuesync.sqlite3").exists()


def test_cache_size_limit_comes_from_the_environment(monkeypatch):
    assert app_module.cache_max_bytes(None) == 500 * 1024 * 1024
    assert app_module.cache_max_bytes("  ") == 500 * 1024 * 1024
    assert app_module.cache_max_bytes("2") == 2 * 1024 * 1024
    monkeypatch.setenv("CUESYNC_CACHE_MAX_MB", "7")
    assert create_app().state.cache._max_bytes == 7 * 1024 * 1024


@pytest.mark.parametrize("value", ["0", "-5", "abc", "1.5"])
def test_cache_size_limit_must_be_a_positive_whole_number(value, monkeypatch):
    with pytest.raises(ValueError, match="CUESYNC_CACHE_MAX_MB"):
        app_module.cache_max_bytes(value)
    monkeypatch.setenv("CUESYNC_CACHE_MAX_MB", value)
    with pytest.raises(ValueError, match="CUESYNC_CACHE_MAX_MB"):
        create_app()


@pytest.mark.parametrize("key", ["garbage", "e30", "WyJhIl0"])
def test_srt_endpoint_rejects_malformed_keys(key):
    assert client.get(f"/{TOKEN}/sub/{key}.srt").status_code == 404


def test_srt_endpoint_rejects_unconfigured_source():
    source = FakeSource()
    response = fake_client(source).get(srt_path(source="subdl"))
    assert response.status_code == 404
    assert source.downloads == []


def test_srt_endpoint_reports_source_errors_as_502():
    source = FakeSource(error="OpenSubtitles download quota used up (HTTP 406)")
    test_client = fake_client(source)
    response = test_client.get(srt_path())
    assert response.status_code == 502
    assert "quota" in response.json()["detail"]
    # A failed download is not remembered, so the next fetch tries again.
    test_client.get(srt_path())
    assert len(source.downloads) == 2


def test_srt_endpoint_rejects_invalid_config():
    response = client.get(f"/{BAD_TOKEN}/sub/x.srt")
    assert response.status_code == 400
    assert API_KEY not in response.text
