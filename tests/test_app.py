import pytest
from fastapi.testclient import TestClient

from cuesync import __version__
from cuesync import app as app_module
from cuesync.app import create_app
from cuesync.stremio import Extra, parse_extra

client = TestClient(create_app())


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_manifest_declares_subtitles_for_imdb_ids():
    response = client.get("/manifest.json")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["id"] == "community.cuesync"
    assert manifest["version"] == __version__
    assert manifest["resources"] == ["subtitles"]
    assert manifest["idPrefixes"] == ["tt"]


def test_manifest_allows_cross_origin_requests():
    response = client.get("/manifest.json", headers={"Origin": "https://web.stremio.com"})
    assert response.headers["access-control-allow-origin"] == "*"


def test_subtitles_without_extra():
    response = client.get("/subtitles/movie/tt1375666.json")
    assert response.json() == {"subtitles": []}


def test_subtitles_with_extra():
    response = client.get(
        "/subtitles/series/tt0903747:1:2/videoHash=abc&videoSize=123&filename=a%20b.mkv.json"
    )
    assert response.json() == {"subtitles": []}


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
        "/subtitles/movie/tt1375666/"
        "videoHash=abc&videoSize=123&filename=a%26b%3Dc%2Fd%20e%2Bf%C3%A9.mkv.json"
    )
    assert response.status_code == 200
    assert response.json() == {"subtitles": []}
    assert parsed == [Extra(video_hash="abc", video_size=123, filename="a&b=c/d e+fé.mkv")]


def test_subtitles_for_episode_with_extra(parsed):
    response = client.get(
        "/subtitles/series/tt0903747:1:2/videoHash=x&videoSize=9&filename=s01e02.mkv.json"
    )
    assert response.status_code == 200
    assert parsed == [Extra(video_hash="x", video_size=9, filename="s01e02.mkv")]


@pytest.mark.parametrize("id", ["abc", "tt123:1", "tt1:a:b", "tt1:1:2:3"])
def test_subtitles_reject_malformed_id(id):
    assert client.get(f"/subtitles/series/{id}.json").status_code == 400


def test_subtitles_with_extra_reject_malformed_id():
    assert client.get("/subtitles/series/tt0903747:1/videoHash=x.json").status_code == 400
