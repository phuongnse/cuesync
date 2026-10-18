"""TorBox client: turn the torrent and file behind the played video into a direct link."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx

from cuesync import __version__
from cuesync.debrid.files import TorrentFile, find_file
from cuesync.stream import StreamRef

API_URL = "https://api.torbox.app/v1/api"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_WAIT = 10.0  # seconds to wait for an added torrent to be ready
POLL_INTERVAL = 1.0  # seconds
LINK_TTL = 60 * 60  # seconds
# Torrent states after which waiting is pointless.
FAILED_STATES = frozenset({"error", "failed"})
_ERROR_CODE = re.compile(r"^[A-Z_]{1,40}$")

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and the link request carries the API key in its query.
logging.getLogger("httpx").setLevel(logging.WARNING)

# (api_key, info_hash, file_idx, casefolded filename) -> (expires_at on the monotonic clock, url)
_links: dict[tuple[str, str, int | None, str | None], tuple[float, str]] = {}


def clear_cache() -> None:
    _links.clear()


class _DebridError(Exception):
    """A step failed. The reason is readable and never holds the URL, headers, key or body."""


class TorBox:
    name = "torbox"

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        *,
        max_wait: float = MAX_WAIT,
        poll_interval: float = POLL_INTERVAL,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._max_wait = max_wait
        self._poll_interval = poll_interval

    async def direct_url(self, stream: StreamRef) -> str | None:
        """Direct HTTPS link to the file. Failures are logged and give None."""
        cache_key = (
            self._api_key,
            stream.info_hash,
            stream.file_idx,
            stream.filename.casefold() if stream.filename else None,
        )
        now = time.monotonic()
        cached = _links.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]

        try:
            url = await self._resolve(stream)
        except _DebridError as exc:
            log.warning("TorBox link failed: %s", exc)
            return None
        if url is None:
            return None
        for key in [key for key, (expires, _) in _links.items() if expires <= now]:
            del _links[key]
        _links[cache_key] = (now + LINK_TTL, url)
        return url

    async def _resolve(self, stream: StreamRef) -> str | None:
        deadline = time.monotonic() + self._max_wait
        existing = await self._existing(stream)
        if existing is not None:
            return await self._download(existing, stream, deadline)

        if not await self._is_cached(stream):
            log.info("TorBox has not cached torrent %s", stream.info_hash)
            return None

        torrent_id = await self._add_magnet(stream)
        if torrent_id is None:
            log.info("TorBox queued torrent %s", stream.info_hash)
            return None
        try:
            url = await self._download({"id": torrent_id}, stream, deadline)
        except _DebridError:
            await self._discard(torrent_id)
            raise
        if url is None:
            log.info("TorBox has not cached torrent %s", stream.info_hash)
            await self._discard(torrent_id)
        return url

    async def _existing(self, stream: StreamRef) -> dict[str, Any] | None:
        """The torrent of the account with the same info hash, in any state."""
        listing = await self._send(
            "list torrents", "GET", "/torrents/mylist", params={"bypass_cache": "true"}
        )
        if listing is None:
            return None
        if not isinstance(listing, list):
            raise _unexpected()
        for entry in listing:
            if not isinstance(entry, dict):
                continue
            info_hash = entry.get("hash")
            if isinstance(info_hash, str) and info_hash.lower() == stream.info_hash:
                _torrent_id(entry)
                return entry
        return None

    async def _is_cached(self, stream: StreamRef) -> bool:
        data = await self._send(
            "check the cache",
            "GET",
            "/torrents/checkcached",
            params={"hash": stream.info_hash, "format": "list", "list_files": "false"},
        )
        if data is None:
            return False
        if isinstance(data, dict):
            return any(isinstance(k, str) and k.lower() == stream.info_hash for k in data)
        if isinstance(data, list):
            return any(
                isinstance(item, dict)
                and isinstance(item.get("hash"), str)
                and item["hash"].lower() == stream.info_hash
                for item in data
            )
        raise _unexpected()

    async def _add_magnet(self, stream: StreamRef) -> int | None:
        """Add the torrent. None when TorBox queued it instead of adding it."""
        added = await self._send(
            "add the magnet",
            "POST",
            "/torrents/createtorrent",
            files={
                "magnet": (None, f"magnet:?xt=urn:btih:{stream.info_hash}"),
                "allow_zip": (None, "false"),
            },
        )
        if not isinstance(added, dict):
            raise _unexpected()
        torrent_id = added.get("torrent_id")
        if torrent_id is None and isinstance(added.get("queued_id"), int):
            return None
        if not _is_int(torrent_id):
            raise _unexpected()
        return torrent_id

    async def _download(
        self, torrent: dict[str, Any], stream: StreamRef, deadline: float
    ) -> str | None:
        """Wait until the torrent is ready, then request the link. None when it is not ready."""
        torrent_id = _torrent_id(torrent)
        known = torrent if "download_present" in torrent else None
        info = await self._wait(torrent_id, known, deadline)
        if info is None:
            return None
        files = _files(info)
        match = find_file(files, stream)
        if match is None:
            log.warning("TorBox torrent %s has no file that matches the stream", stream.info_hash)
            raise _DebridError("the file is not in the torrent")
        return await self._request_link(torrent_id, match.index)

    async def _wait(
        self, torrent_id: int, info: dict[str, Any] | None, deadline: float
    ) -> dict[str, Any] | None:
        while True:
            if info is None:
                info = await self._info(torrent_id)
            state = info.get("download_state")
            if isinstance(state, str) and state.lower() in FAILED_STATES:
                raise _DebridError(f"TorBox reports the torrent as {state}")
            if info.get("download_present") is True:
                return info
            if time.monotonic() >= deadline:
                return None
            info = None
            await asyncio.sleep(self._poll_interval)

    async def _info(self, torrent_id: int) -> dict[str, Any]:
        data = await self._send(
            "read torrent",
            "GET",
            "/torrents/mylist",
            params={"id": torrent_id, "bypass_cache": "true"},
        )
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if not isinstance(data, dict):
            raise _unexpected()
        return data

    async def _request_link(self, torrent_id: int, file_id: int) -> str:
        data = await self._send(
            "request the link",
            "GET",
            "/torrents/requestdl",
            params={
                "token": self._api_key,
                "torrent_id": torrent_id,
                "file_id": file_id,
                "zip_link": "false",
                "redirect": "false",
            },
        )
        if not isinstance(data, str) or not data.startswith("https://"):
            raise _DebridError("TorBox gave no HTTPS download link")
        return data

    async def _discard(self, torrent_id: int) -> None:
        """Remove a torrent CueSync added. Best effort."""
        try:
            await self._send(
                "delete torrent",
                "POST",
                "/torrents/controltorrent",
                json={"torrent_id": torrent_id, "operation": "delete"},
            )
        except _DebridError as exc:
            log.debug("TorBox could not delete the torrent it added: %s", exc)

    async def _send(self, what: str, method: str, path: str, **kwargs: Any) -> Any:
        """Call the API and return the `data` of the answer."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": f"CueSync v{__version__}",
            "Accept": "application/json",
        }
        try:
            response = await self._client.request(
                method, API_URL + path, headers=headers, timeout=TIMEOUT, **kwargs
            )
        except httpx.TimeoutException:
            raise _DebridError(f"TorBox timed out ({what})") from None
        except httpx.HTTPError as exc:
            raise _DebridError(f"TorBox request failed ({what}, {type(exc).__name__})") from None
        code = response.status_code
        if code == 401:
            raise _DebridError("TorBox rejected the API key (HTTP 401)")
        if code == 403:
            raise _DebridError("TorBox refused access (HTTP 403), the key or plan may not allow it")
        if code == 429:
            raise _DebridError("TorBox rate limit (HTTP 429)")
        if response.is_error:
            raise _DebridError(f"TorBox returned HTTP {code}")
        try:
            body = response.json()
        except ValueError:
            raise _DebridError(f"TorBox sent an answer that is not JSON ({what})") from None
        if not isinstance(body, dict) or not isinstance(body.get("success"), bool):
            raise _unexpected()
        if not body["success"]:
            error = body.get("error")
            code_name = error if isinstance(error, str) and _ERROR_CODE.match(error) else "unknown"
            raise _DebridError(f"TorBox refused the request ({what}, {code_name})")
        return body.get("data")


def _unexpected() -> _DebridError:
    return _DebridError("TorBox sent an unexpected answer")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _torrent_id(entry: dict[str, Any]) -> int:
    torrent_id = entry.get("id")
    if not _is_int(torrent_id):
        torrent_id = entry.get("torrent_id")
    if not _is_int(torrent_id):
        raise _unexpected()
    return torrent_id


def _files(info: dict[str, Any]) -> list[TorrentFile]:
    """The `files` of a torrent, checked for the fields used here."""
    files = info.get("files")
    if not isinstance(files, list):
        raise _unexpected()
    result = []
    for file in files:
        if (
            not isinstance(file, dict)
            or not _is_int(file.get("id"))
            or not isinstance(file.get("name"), str)
            or not _is_int(file.get("size"))
        ):
            raise _unexpected()
        result.append(TorrentFile(index=file["id"], path=file["name"], size=file["size"]))
    return result
