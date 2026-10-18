from fastapi.testclient import TestClient

from cuesync import __version__
from cuesync.app import create_app, parse_extra

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


def test_parse_extra():
    assert parse_extra("videoHash=abc&videoSize=123&filename=a%20b.mkv") == {
        "videoHash": "abc",
        "videoSize": "123",
        "filename": "a b.mkv",
    }
