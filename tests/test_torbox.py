"""Tests for the TorBox client. HTTP is faked with httpx.MockTransport.

The answers are shaped after the TorBox API documentation (https://api-docs.torbox.app, its
OpenAPI description) as read when the client was written, not recorded from the service. The
documentation does not spell out the `mylist` answer, so its fields (`id`, `hash`,
`download_present`, `download_state`, `files[].id/name/size`) follow the API as the client uses it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from cuesync import __version__
from cuesync.debrid import torbox as torbox_module
from cuesync.debrid.torbox import TorBox
from cuesync.stream import StreamRef

KEY = "SECRET-TB-KEY"
HASH = "e10c8eb30c64876fb82a9a49e2b9186e42191fe2"
MOVIE_BYTES = 5_000_000
STREAM = StreamRef(HASH, None, "Movie.2010.mkv", MOVIE_BYTES)
LINK = "https://store.torbox.example/dl/FILE"
PACK = [
    {"id": 0, "name": "Show/Show.S01E01.mkv", "size": 100},
    {"id": 1, "name": "Show/Show.S01E02.mkv", "size": 200},
    {"id": 2, "name": "Show/Show.S01E03.mkv", "size": 300},
    {"id": 3, "name": "Show/Show.nfo", "size": 5},
]
SINGLE = [{"id": 0, "name": "Movie.2010.mkv", "size": MOVIE_BYTES}]
NEVER = 10**6


def envelope(data: Any, *, success: bool = True, error: str | None = None) -> dict[str, Any]:
    return {
        "success": success,
        "error": error,
        "detail": "Some detail that must not be logged.",
        "data": data,
    }


def torrent(
    files: list[dict[str, Any]] = SINGLE,
    *,
    torrent_id: int = 7,
    ready: bool = True,
    state: str | None = None,
) -> dict[str, Any]:
    return {
        "id": torrent_id,
        "hash": HASH.upper(),
        "download_present": ready,
        "download_state": state or ("cached" if ready else "downloading"),
        "files": [dict(f) for f in files],
        "ready_after": 0 if ready else NEVER,
    }


class FakeTorBox:
    """A stateful fake of the TorBox API that records requests.

    `cached` is whether TorBox has the torrent cached. `existing` are torrents already in the
    account. A torrent turns `download_present` after `ready_after` polls of its own `mylist`.
    """

    def __init__(
        self,
        files: list[dict[str, Any]] = SINGLE,
        *,
        cached: bool = True,
        existing: list[dict[str, Any]] | None = None,
        fail: dict[str, int] | None = None,
        ready_after: int = 0,
        queue: bool = False,
        link: str = LINK,
    ) -> None:
        self.files = files
        self.cached = cached
        self.torrents: dict[int, dict[str, Any]] = {t["id"]: t for t in existing or []}
        self.fail = fail or {}  # "METHOD path-prefix" -> status
        self.ready_after = ready_after
        self.queue = queue
        self.link = link
        self.requests: list[httpx.Request] = []

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path.removeprefix('/v1/api')}" for r in self.requests]

    def request(self, call: str) -> httpx.Request:
        return self.requests[self.paths().index(call)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path.removeprefix("/v1/api")
        query = dict(request.url.params)
        for rule, status in self.fail.items():
            rule_method, prefix = rule.split(" ", 1)
            if method == rule_method and path.startswith(prefix):
                return httpx.Response(status, json=envelope(None, success=False, error="NOPE"))
        if method == "GET" and path == "/torrents/mylist":
            if "id" in query:
                found = self.torrents.get(int(query["id"]))
                if found is None:
                    return httpx.Response(200, json=envelope(None))
                found["polls"] = found.get("polls", 0) + 1
                if found["polls"] > found["ready_after"]:
                    found["download_present"] = True
                    found["download_state"] = "cached"
                return httpx.Response(200, json=envelope(self._public(found)))
            return httpx.Response(
                200, json=envelope([self._public(t) for t in self.torrents.values()])
            )
        if method == "GET" and path == "/torrents/checkcached":
            assert query["hash"] == HASH
            data = [{"hash": HASH, "name": "Movie", "size": MOVIE_BYTES}] if self.cached else []
            return httpx.Response(200, json=envelope(data))
        if method == "POST" and path == "/torrents/createtorrent":
            assert request.headers["content-type"].startswith("multipart/form-data")
            assert b"magnet:?xt=urn:btih:" + HASH.encode() in request.content
            assert b'name="allow_zip"' in request.content
            if self.queue:
                return httpx.Response(200, json=envelope({"queued_id": 3, "hash": HASH}))
            torrent_id = max(self.torrents, default=0) + 1
            self.torrents[torrent_id] = torrent(self.files, torrent_id=torrent_id, ready=False) | {
                "ready_after": self.ready_after
            }
            return httpx.Response(200, json=envelope({"torrent_id": torrent_id, "hash": HASH}))
        if method == "GET" and path == "/torrents/requestdl":
            assert request.headers["Authorization"] == f"Bearer {query['token']}"
            assert query["zip_link"] == "false"
            assert int(query["torrent_id"]) in self.torrents
            return httpx.Response(200, json=envelope(self.link))
        if method == "POST" and path == "/torrents/controltorrent":
            body = json.loads(request.content)
            assert body["operation"] == "delete"
            self.torrents.pop(body["torrent_id"], None)
            return httpx.Response(200, json=envelope(None))
        return httpx.Response(404)

    @staticmethod
    def _public(found: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in found.items() if k not in {"ready_after", "polls"}}


@pytest.fixture(autouse=True)
def clean():
    torbox_module.clear_cache()
    yield
    torbox_module.clear_cache()


def resolve_with(
    handler: Callable[[httpx.Request], httpx.Response], *streams: StreamRef, key: str = KEY
) -> list[str | None]:
    async def main():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = TorBox(key, client, max_wait=0.05, poll_interval=0.0)
            return [await provider.direct_url(stream) for stream in streams]

    return asyncio.run(main())


def resolve(api: FakeTorBox, *streams: StreamRef, key: str = KEY) -> list[str | None]:
    return resolve_with(api, *streams, key=key)


def test_happy_path_checks_adds_waits_and_requests_the_link():
    api = FakeTorBox(SINGLE, ready_after=1)
    assert resolve(api, STREAM) == [LINK]
    paths = api.paths()
    assert paths[:3] == [
        "GET /torrents/mylist",
        "GET /torrents/checkcached",
        "POST /torrents/createtorrent",
    ]
    assert paths[3:-1] == ["GET /torrents/mylist"] * (len(paths) - 4)
    assert paths[-1] == "GET /torrents/requestdl"
    assert not any(p.startswith("POST /torrents/controltorrent") for p in paths)
    add = api.request("POST /torrents/createtorrent")
    assert add.headers["Authorization"] == f"Bearer {KEY}"
    assert add.headers["User-Agent"] == f"CueSync v{__version__}"
    assert api.request("GET /torrents/requestdl").url.params["file_id"] == "0"


def test_reuses_a_ready_torrent_without_adding():
    api = FakeTorBox(PACK, existing=[torrent(PACK, torrent_id=9)])
    stream = StreamRef(HASH, 0, "Show.S01E02.mkv", 200)
    assert resolve(api, stream) == [LINK]
    assert api.paths() == ["GET /torrents/mylist", "GET /torrents/requestdl"]
    params = api.request("GET /torrents/requestdl").url.params
    assert params["torrent_id"] == "9"
    assert params["file_id"] == "1"
    assert params["redirect"] == "false"


def test_waits_for_an_existing_torrent_that_is_downloading_and_never_deletes_it():
    old = torrent(SINGLE, torrent_id=9, ready=False) | {"ready_after": 2}
    api = FakeTorBox(existing=[old])
    assert resolve_with(api, STREAM) == [LINK]
    assert "POST /torrents/createtorrent" not in api.paths()
    assert "POST /torrents/controltorrent" not in api.paths()

    stuck = FakeTorBox(existing=[torrent(SINGLE, torrent_id=9, ready=False)])
    assert resolve(stuck, STREAM, key=KEY + "2") == [None]
    assert "POST /torrents/controltorrent" not in stuck.paths()
    assert 9 in stuck.torrents


def test_an_existing_torrent_in_a_failed_state_is_kept(caplog):
    api = FakeTorBox(existing=[torrent(SINGLE, torrent_id=9, ready=False, state="error")])
    assert resolve(api, STREAM) == [None]
    assert "TorBox reports the torrent as error" in caplog.text
    assert 9 in api.torrents


def test_not_cached_gives_none_adds_nothing_and_logs(caplog):
    api = FakeTorBox(cached=False)
    with caplog.at_level(logging.INFO, logger="cuesync.debrid.torbox"):
        assert resolve(api, STREAM) == [None]
    assert api.paths() == ["GET /torrents/mylist", "GET /torrents/checkcached"]
    assert f"TorBox has not cached torrent {HASH}" in caplog.text


def test_not_cached_answer_as_an_object_or_null_gives_none():
    for data in ({}, None):
        torbox_module.clear_cache()
        assert resolve_with(
            lambda request, data=data: httpx.Response(
                200,
                json=envelope(
                    data if request.url.path.endswith("checkcached") else [],
                ),
            ),
            STREAM,
        ) == [None]


def test_queued_torrent_gives_none(caplog):
    api = FakeTorBox(queue=True)
    with caplog.at_level(logging.INFO, logger="cuesync.debrid.torbox"):
        assert resolve(api, STREAM) == [None]
    assert f"TorBox queued torrent {HASH}" in caplog.text
    assert "POST /torrents/controltorrent" not in api.paths()


def test_added_torrent_that_is_never_ready_is_deleted(caplog):
    api = FakeTorBox(ready_after=NEVER)
    with caplog.at_level(logging.INFO, logger="cuesync.debrid.torbox"):
        assert resolve(api, STREAM) == [None]
    assert "POST /torrents/controltorrent" in api.paths()
    assert api.torrents == {}
    assert "GET /torrents/requestdl" not in api.paths()
    assert f"TorBox has not cached torrent {HASH}" in caplog.text


def test_failed_state_deletes_the_added_torrent(caplog):
    api = FakeTorBox()
    original = api.__call__

    def broken(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("/createtorrent"):
            api.torrents[1].update(download_state="failed", ready_after=NEVER)
        return response

    assert resolve_with(broken, STREAM) == [None]
    assert "TorBox reports the torrent as failed" in caplog.text
    assert "POST /torrents/controltorrent" in api.paths()


def test_invalid_key_gives_none_and_never_logs_the_key(caplog):
    api = FakeTorBox(fail={"GET /torrents/mylist": 401})
    with caplog.at_level(logging.DEBUG):
        assert resolve(api, STREAM) == [None]
    assert "rejected the API key (HTTP 401)" in caplog.text
    assert KEY not in caplog.text
    assert api.paths() == ["GET /torrents/mylist"]


def test_forbidden_gives_none(caplog):
    api = FakeTorBox(fail={"GET /torrents/mylist": 403})
    assert resolve(api, STREAM) == [None]
    assert "refused access (HTTP 403)" in caplog.text


def test_link_request_error_deletes_the_torrent_and_never_logs_the_key(caplog):
    api = FakeTorBox(fail={"GET /torrents/requestdl": 503})
    with caplog.at_level(logging.DEBUG):
        assert resolve(api, STREAM) == [None]
    assert "TorBox returned HTTP 503" in caplog.text
    assert "POST /torrents/controltorrent" in api.paths()
    assert KEY not in caplog.text


def test_rate_limit_gives_none(caplog):
    api = FakeTorBox(fail={"POST /torrents/createtorrent": 429})
    assert resolve(api, STREAM) == [None]
    assert "rate limit (HTTP 429)" in caplog.text
    assert "POST /torrents/controltorrent" not in api.paths()


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


def test_refused_request_logs_the_error_code_but_not_the_detail(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(None, success=False, error="BAD_TOKEN"))

    with caplog.at_level(logging.DEBUG):
        assert resolve_with(handler, STREAM) == [None]
    assert "TorBox refused the request (list torrents, BAD_TOKEN)" in caplog.text
    assert "must not be logged" not in caplog.text


def test_refused_request_with_a_free_text_error_logs_unknown(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=envelope(None, success=False, error="secret token abc"))

    assert resolve_with(handler, STREAM) == [None]
    assert "(list torrents, unknown)" in caplog.text
    assert "abc" not in caplog.text


def test_answer_of_the_wrong_shape_gives_none(caplog):
    assert resolve_with(lambda request: httpx.Response(200, json={"a": 1}), STREAM) == [None]
    assert "unexpected answer" in caplog.text


def test_link_that_is_not_https_gives_none(caplog):
    api = FakeTorBox(link="http://store.torbox.example/dl/FILE")
    assert resolve(api, STREAM) == [None]
    assert "no HTTPS download link" in caplog.text
    assert "POST /torrents/controltorrent" in api.paths()


def test_second_call_is_served_from_the_cache():
    api = FakeTorBox()
    first = resolve(api, STREAM)
    count = len(api.requests)
    assert resolve(api, STREAM) == first
    assert len(api.requests) == count


def test_the_cache_is_per_key_and_expires(monkeypatch):
    api = FakeTorBox(existing=[torrent()])
    resolve(api, STREAM)
    count = len(api.requests)
    resolve(api, STREAM, key="OTHER")
    assert len(api.requests) > count
    count = len(api.requests)
    now = torbox_module.time.monotonic()
    monkeypatch.setattr(torbox_module.time, "monotonic", lambda: now + 2 * 60 * 60)
    resolve(api, STREAM)
    assert len(api.requests) > count


def requested_file(api: FakeTorBox) -> str:
    return api.request("GET /torrents/requestdl").url.params["file_id"]


def test_pack_file_is_chosen_by_name():
    # The file index points at another file, the name wins.
    stream = StreamRef(HASH, 0, "show.s01e03.MKV", None)
    api = FakeTorBox(PACK)
    assert resolve(api, stream) == [LINK]
    assert requested_file(api) == "2"


def test_pack_file_falls_back_to_the_zero_based_index():
    stream = StreamRef(HASH, 1, "Other.Name.mkv", None)
    api = FakeTorBox(PACK)
    assert resolve(api, stream) == [LINK]
    assert requested_file(api) == "1"


def test_pack_file_falls_back_to_the_size():
    stream = StreamRef(HASH, None, "Other.Name.mkv", 300)
    api = FakeTorBox(PACK)
    assert resolve(api, stream) == [LINK]
    assert requested_file(api) == "2"


def test_no_matching_file_gives_none_and_deletes_the_torrent(caplog):
    stream = StreamRef(HASH, 9, "Missing.mkv", None)
    api = FakeTorBox(PACK)
    assert resolve(api, stream) == [None]
    assert "no file that matches" in caplog.text
    assert "POST /torrents/controltorrent" in api.paths()
    assert api.torrents == {}


def test_no_matching_file_in_an_existing_torrent_keeps_it():
    stream = StreamRef(HASH, 9, "Missing.mkv", None)
    api = FakeTorBox(PACK, existing=[torrent(PACK, torrent_id=9)])
    assert resolve(api, stream) == [None]
    assert "POST /torrents/controltorrent" not in api.paths()
