import logging

import pytest
from fastapi.testclient import TestClient

from cuesync import __version__
from cuesync import app as app_module
from cuesync.app import create_app
from cuesync.config import UserConfig, encode_config
from cuesync.stremio import Extra, parse_extra

client = TestClient(create_app())

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
        for field in UserConfig.model_fields:
            assert f'name="{field}"' in response.text


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
