"""Tests for the Real-Debrid client. HTTP is faked with httpx.MockTransport.

The answers are shaped after the Real-Debrid API documentation, not recorded from the service.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from cuesync import __version__
from cuesync.config import UserConfig
from cuesync.debrid import DebridProvider, RealDebrid, debrid_provider
from cuesync.debrid import realdebrid as realdebrid_module
from cuesync.stream import StreamRef

KEY = "SECRET-RD-KEY"
HASH = "e10c8eb30c64876fb82a9a49e2b9186e42191fe2"
MOVIE_BYTES = 5_000_000
STREAM = StreamRef(HASH, None, "Movie.2010.mkv", MOVIE_BYTES)
PACK = [
    {"id": 1, "path": "/Show/Show.S01E01.mkv", "bytes": 100, "selected": 0},
    {"id": 2, "path": "/Show/Show.S01E02.mkv", "bytes": 200, "selected": 0},
    {"id": 3, "path": "/Show/Show.S01E03.mkv", "bytes": 300, "selected": 0},
    {"id": 4, "path": "/Show/Show.nfo", "bytes": 5, "selected": 0},
]
SINGLE = [{"id": 1, "path": "/Movie.2010.mkv", "bytes": MOVIE_BYTES, "selected": 0}]


class FakeRealDebrid:
    """A stateful fake of the Real-Debrid API that records requests.

    `statuses` is the status sequence of a torrent added by addMagnet. The last one repeats.
    Selecting files turns the torrent into `downloaded` when `cached` is set.
    """

    def __init__(
        self,
        files: list[dict[str, Any]] = SINGLE,
        *,
        cached: bool = True,
        existing: list[dict[str, Any]] | None = None,
        fail: dict[str, int] | None = None,
        download: str = "https://dl.real-debrid.example/d/FILE",
    ) -> None:
        self.files = [dict(f) for f in files]
        self.cached = cached
        self.torrents: dict[str, dict[str, Any]] = {t["id"]: t for t in existing or []}
        self.fail = fail or {}  # "METHOD path-prefix" -> status
        self.download = download
        self.requests: list[httpx.Request] = []
        self.forms: list[dict[str, str]] = []
        self.unrestricted: list[str] = []

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path.removeprefix('/rest/1.0')}" for r in self.requests]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.forms.append(form)
        path = request.url.path.removeprefix("/rest/1.0")
        for rule, status in self.fail.items():
            method, prefix = rule.split(" ", 1)
            if request.method == method and path.startswith(prefix):
                return httpx.Response(status, json={"error": "nope", "error_code": status})
        method = request.method
        if method == "GET" and path == "/torrents":
            if not self.torrents:
                return httpx.Response(204)
            return httpx.Response(
                200,
                json=[
                    {"id": t["id"], "hash": t["hash"].upper(), "status": t["status"]}
                    for t in self.torrents.values()
                ],
            )
        if method == "GET" and path.startswith("/torrents/info/"):
            torrent = self.torrents.get(path.rsplit("/", 1)[1])
            if torrent is None:
                return httpx.Response(404, json={"error": "unknown_ressource"})
            return httpx.Response(200, json=self._info(torrent))
        if method == "POST" and path == "/torrents/addMagnet":
            torrent_id = f"T{len(self.torrents) + 1}"
            self.torrents[torrent_id] = {
                "id": torrent_id,
                "hash": form["magnet"].rsplit(":", 1)[1],
                "status": "waiting_files_selection",
                "files": [dict(f) for f in self.files],
                "links": [],
            }
            return httpx.Response(201, json={"id": torrent_id, "uri": "x"})
        if method == "POST" and path.startswith("/torrents/selectFiles/"):
            torrent = self.torrents[path.rsplit("/", 1)[1]]
            ids = {int(i) for i in form["files"].split(",")}
            for file in torrent["files"]:
                file["selected"] = int(file["id"] in ids)
            torrent["status"] = "downloaded" if self.cached else "downloading"
            torrent["links"] = [
                f"https://real-debrid.example/d/{torrent['id']}-{i}" for i in sorted(ids)
            ]
            return httpx.Response(204)
        if method == "DELETE" and path.startswith("/torrents/delete/"):
            self.torrents.pop(path.rsplit("/", 1)[1], None)
            return httpx.Response(204)
        if method == "POST" and path == "/unrestrict/link":
            self.unrestricted.append(form["link"])
            return httpx.Response(200, json={"download": self.download})
        return httpx.Response(404)

    @staticmethod
    def _info(torrent: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": torrent["id"],
            "hash": torrent["hash"],
            "status": torrent["status"],
            "files": torrent["files"],
            "links": torrent["links"],
        }


def downloaded_torrent(
    files: list[dict[str, Any]], selected: list[int], torrent_id: str = "OLD"
) -> dict[str, Any]:
    chosen = [dict(f, selected=int(f["id"] in selected)) for f in files]
    return {
        "id": torrent_id,
        "hash": HASH,
        "status": "downloaded",
        "files": chosen,
        "links": [f"https://real-debrid.example/d/{torrent_id}-{i}" for i in sorted(selected)],
    }


@pytest.fixture(autouse=True)
def clean():
    realdebrid_module.clear_cache()
    yield
    realdebrid_module.clear_cache()


def resolve(api: FakeRealDebrid, *streams: StreamRef, key: str = KEY) -> list[str | None]:
    async def main():
        transport = httpx.MockTransport(api)
        async with httpx.AsyncClient(transport=transport) as client:
            provider = RealDebrid(key, client, max_wait=0.05, poll_interval=0.0)
            return [await provider.direct_url(stream) for stream in streams]

    return asyncio.run(main())


def test_happy_path_adds_selects_and_unrestricts():
    api = FakeRealDebrid(SINGLE)
    assert resolve(api, STREAM) == ["https://dl.real-debrid.example/d/FILE"]
    paths = api.paths()
    assert paths[0] == "GET /torrents"
    assert "POST /torrents/addMagnet" in paths
    assert "POST /torrents/selectFiles/T1" in paths
    assert paths[-1] == "POST /unrestrict/link"
    assert "DELETE /torrents/delete/T1" not in paths
    add = api.requests[paths.index("POST /torrents/addMagnet")]
    assert api.forms[paths.index("POST /torrents/addMagnet")] == {
        "magnet": f"magnet:?xt=urn:btih:{HASH}"
    }
    assert add.headers["Authorization"] == f"Bearer {KEY}"
    assert add.headers["User-Agent"] == f"CueSync v{__version__}"
    assert api.forms[paths.index("POST /torrents/selectFiles/T1")] == {"files": "1"}
    assert api.unrestricted == ["https://real-debrid.example/d/T1-1"]


def test_selects_every_video_file_and_the_target():
    stream = StreamRef(HASH, 1, "Show.S01E02.mkv", 200)
    api = FakeRealDebrid(PACK)
    assert resolve(api, stream) == ["https://dl.real-debrid.example/d/FILE"]
    select = api.paths().index("POST /torrents/selectFiles/T1")
    assert api.forms[select] == {"files": "1,2,3"}
    assert api.unrestricted == ["https://real-debrid.example/d/T1-2"]


def test_reuses_a_downloaded_torrent_without_adding():
    api = FakeRealDebrid(existing=[downloaded_torrent(SINGLE, [1])])
    assert resolve(api, STREAM) == ["https://dl.real-debrid.example/d/FILE"]
    assert api.paths() == [
        "GET /torrents",
        "GET /torrents/info/OLD",
        "POST /unrestrict/link",
    ]
    assert api.unrestricted == ["https://real-debrid.example/d/OLD-1"]


def test_does_not_reuse_a_torrent_without_the_file_selected():
    api = FakeRealDebrid(PACK, existing=[downloaded_torrent(PACK, [1])])
    stream = StreamRef(HASH, 1, "Show.S01E02.mkv", 200)
    assert resolve(api, stream) == ["https://dl.real-debrid.example/d/FILE"]
    assert "POST /torrents/addMagnet" in api.paths()
    assert "OLD" in api.torrents  # the user's torrent stays


def test_does_not_reuse_a_torrent_that_is_not_downloaded():
    old = downloaded_torrent(SINGLE, [1])
    old["status"] = "downloading"
    api = FakeRealDebrid(existing=[old])
    assert resolve(api, STREAM) == ["https://dl.real-debrid.example/d/FILE"]
    assert "POST /torrents/addMagnet" in api.paths()


def test_not_cached_gives_none_deletes_the_torrent_and_logs(caplog):
    api = FakeRealDebrid(SINGLE, cached=False)
    with caplog.at_level(logging.INFO, logger="cuesync.debrid.realdebrid"):
        assert resolve(api, STREAM) == [None]
    assert "DELETE /torrents/delete/T1" in api.paths()
    assert "T1" not in api.torrents
    assert f"Real-Debrid has not cached torrent {HASH}" in caplog.text
    assert "POST /unrestrict/link" not in api.paths()


def test_a_failed_torrent_is_deleted_at_once():
    api = FakeRealDebrid(SINGLE)
    original = api.__call__

    def broken(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("/selectFiles/T1"):
            api.torrents["T1"]["status"] = "magnet_error"
        return response

    assert resolve_with(broken, STREAM) == [None]
    assert "DELETE /torrents/delete/T1" in api.paths()


def resolve_with(handler: Callable[[httpx.Request], httpx.Response], *streams: StreamRef):
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = RealDebrid(KEY, client, max_wait=0.05, poll_interval=0.0)
            return [await provider.direct_url(stream) for stream in streams]

    return asyncio.run(main())


def test_invalid_key_gives_none_and_never_logs_the_key(caplog):
    api = FakeRealDebrid(fail={"GET /torrents": 401})
    with caplog.at_level(logging.DEBUG):
        assert resolve(api, STREAM) == [None]
    assert "rejected the API key (HTTP 401)" in caplog.text
    assert KEY not in caplog.text
    assert api.paths() == ["GET /torrents"]


def test_forbidden_names_the_premium_requirement(caplog):
    api = FakeRealDebrid(fail={"GET /torrents": 403})
    assert resolve(api, STREAM) == [None]
    assert "not premium" in caplog.text


def test_rate_limit_gives_none(caplog):
    api = FakeRealDebrid(fail={"POST /torrents/addMagnet": 429})
    assert resolve(api, STREAM) == [None]
    assert "rate limit (HTTP 429)" in caplog.text
    assert not any(p.startswith("DELETE") for p in api.paths())


def test_server_error_after_adding_deletes_the_torrent(caplog):
    api = FakeRealDebrid(fail={"POST /unrestrict": 503})
    assert resolve(api, STREAM) == [None]
    assert "Real-Debrid returned HTTP 503" in caplog.text
    assert "DELETE /torrents/delete/T1" in api.paths()


def test_timeout_gives_none_and_never_logs_the_key(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with caplog.at_level(logging.DEBUG):
        assert resolve_with(handler, STREAM) == [None]
    assert "timed out" in caplog.text
    assert KEY not in caplog.text


def test_answer_that_is_not_json_gives_none(caplog):
    assert resolve_with(lambda request: httpx.Response(200, text="<html>"), STREAM) == [None]
    assert "not JSON" in caplog.text


def test_answer_of_the_wrong_shape_gives_none(caplog):
    assert resolve_with(lambda request: httpx.Response(200, json={"a": 1}), STREAM) == [None]
    assert "unexpected answer" in caplog.text


def test_second_call_is_served_from_the_cache():
    api = FakeRealDebrid(SINGLE)
    first = resolve(api, STREAM)
    count = len(api.requests)
    assert resolve(api, STREAM) == first
    assert len(api.requests) == count


def test_the_cache_is_per_key_and_expires(monkeypatch):
    api = FakeRealDebrid(SINGLE)
    resolve(api, STREAM)
    count = len(api.requests)
    resolve(api, STREAM, key="OTHER")
    assert len(api.requests) > count
    count = len(api.requests)
    now = realdebrid_module.time.monotonic()
    monkeypatch.setattr(realdebrid_module.time, "monotonic", lambda: now + 2 * 60 * 60)
    resolve(api, STREAM)
    assert len(api.requests) > count


def test_pack_file_is_chosen_by_name():
    # The file index points at another file, the name wins.
    stream = StreamRef(HASH, 0, "show.s01e03.MKV", None)
    api = FakeRealDebrid(PACK)
    assert resolve(api, stream) == ["https://dl.real-debrid.example/d/FILE"]
    assert api.unrestricted == ["https://real-debrid.example/d/T1-3"]


def test_pack_file_falls_back_to_the_index_plus_one():
    stream = StreamRef(HASH, 1, "Other.Name.mkv", None)
    api = FakeRealDebrid(PACK)
    assert resolve(api, stream) == ["https://dl.real-debrid.example/d/FILE"]
    assert api.unrestricted == ["https://real-debrid.example/d/T1-2"]


def test_pack_file_falls_back_to_the_size():
    stream = StreamRef(HASH, None, "Other.Name.mkv", 300)
    api = FakeRealDebrid(PACK)
    assert resolve(api, stream) == ["https://dl.real-debrid.example/d/FILE"]
    assert api.unrestricted == ["https://real-debrid.example/d/T1-3"]


def test_single_file_torrent_without_any_hint_uses_the_largest_file():
    stream = StreamRef(HASH, None, None, None)
    api = FakeRealDebrid(PACK)
    resolve(api, stream)
    assert api.unrestricted == ["https://real-debrid.example/d/T1-3"]


def test_no_matching_file_gives_none_and_deletes_the_torrent(caplog):
    stream = StreamRef(HASH, 9, "Missing.mkv", None)
    api = FakeRealDebrid(PACK)
    assert resolve(api, stream) == [None]
    assert "no file that matches" in caplog.text
    assert "DELETE /torrents/delete/T1" in api.paths()


def test_index_with_a_different_size_is_rejected():
    stream = StreamRef(HASH, 1, "Other.Name.mkv", 999)
    api = FakeRealDebrid(PACK)
    assert resolve(api, stream) == [None]


def test_archive_gives_none(caplog):
    api = FakeRealDebrid(PACK)
    original = api.__call__

    def archive(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("/selectFiles/T1"):
            api.torrents["T1"]["links"] = ["https://real-debrid.example/d/ARCHIVE"]
        return response

    stream = StreamRef(HASH, 1, "Show.S01E02.mkv", 200)
    assert resolve_with(archive, stream) == [None]
    assert "archive" in caplog.text
    assert "POST /unrestrict/link" not in api.paths()


def test_download_that_is_not_https_gives_none(caplog):
    api = FakeRealDebrid(SINGLE, download="http://dl.real-debrid.example/d/FILE")
    assert resolve(api, STREAM) == [None]
    assert "no HTTPS download link" in caplog.text


def test_factory():
    async def main():
        async with httpx.AsyncClient() as client:
            real = debrid_provider(
                UserConfig(languages=["eng"], debrid_provider="realdebrid", debrid_api_key="k"),
                client,
            )
            torbox = debrid_provider(
                UserConfig(languages=["eng"], debrid_provider="torbox", debrid_api_key="k"),
                client,
            )
            none = debrid_provider(UserConfig(languages=["eng"]), client)
            return real, torbox, none

    real, torbox, none = asyncio.run(main())
    assert isinstance(real, RealDebrid)
    provider: DebridProvider = real
    assert provider.name == "realdebrid"
    assert torbox is None
    assert none is None
