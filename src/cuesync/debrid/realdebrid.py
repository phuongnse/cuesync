"""Real-Debrid client: turn the torrent and file behind the played video into a direct link."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import PurePosixPath
from typing import Any

import httpx

from cuesync import __version__
from cuesync.stream import StreamRef

API_URL = "https://api.real-debrid.com/rest/1.0"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_WAIT = 10.0  # seconds to wait for a torrent to be downloaded, adding and selecting files
POLL_INTERVAL = 1.0  # seconds
LINK_TTL = 60 * 60  # seconds
LIST_LIMIT = 100
VIDEO_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg"}
)
# Torrent states after which waiting is pointless.
FAILED_STATUSES = frozenset({"error", "virus", "dead", "magnet_error"})

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and a request can name the user's torrents.
logging.getLogger("httpx").setLevel(logging.WARNING)

# (api_key, info_hash, file_idx, casefolded filename) -> (expires_at on the monotonic clock, url)
_links: dict[tuple[str, str, int | None, str | None], tuple[float, str]] = {}


def clear_cache() -> None:
    _links.clear()


class _DebridError(Exception):
    """A step failed. The reason is readable and never holds the URL, headers, key or body."""


class RealDebrid:
    name = "realdebrid"

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
            log.warning("Real-Debrid link failed: %s", exc)
            return None
        if url is None:
            return None
        for key in [key for key, (expires, _) in _links.items() if expires <= now]:
            del _links[key]
        _links[cache_key] = (now + LINK_TTL, url)
        return url

    async def _resolve(self, stream: StreamRef) -> str | None:
        existing = await self._existing(stream)
        if existing is not None:
            info, target_id = existing
            return await self._unrestrict(_link_for(info, target_id))

        torrent_id = await self._add_magnet(stream)
        try:
            ready = await self._download(torrent_id, stream)
            if ready is None:
                log.info("Real-Debrid has not cached torrent %s", stream.info_hash)
                await self._discard(torrent_id)
                return None
            info, target_id = ready
            return await self._unrestrict(_link_for(info, target_id))
        except _DebridError:
            await self._discard(torrent_id)
            raise

    async def _existing(self, stream: StreamRef) -> tuple[dict[str, Any], int] | None:
        """A downloaded torrent of the account with the same info hash and the file selected."""
        listing = await self._send(
            "list torrents", "GET", "/torrents", params={"limit": LIST_LIMIT}
        )
        if listing is None:
            return None
        if not isinstance(listing, list):
            raise _unexpected()
        for entry in listing:
            if not isinstance(entry, dict):
                continue
            info_hash, status, torrent_id = entry.get("hash"), entry.get("status"), entry.get("id")
            if (
                not isinstance(info_hash, str)
                or info_hash.lower() != stream.info_hash
                or status != "downloaded"
                or not isinstance(torrent_id, str)
            ):
                continue
            info = await self._info(torrent_id)
            target = _pick_file(_files(info), stream, warn=False)
            if target is not None and target.get("selected") == 1:
                return info, target["id"]
        return None

    async def _add_magnet(self, stream: StreamRef) -> str:
        added = await self._send(
            "add the magnet",
            "POST",
            "/torrents/addMagnet",
            data={"magnet": f"magnet:?xt=urn:btih:{stream.info_hash}"},
        )
        torrent_id = added.get("id") if isinstance(added, dict) else None
        if not isinstance(torrent_id, str) or not torrent_id:
            raise _unexpected()
        return torrent_id

    async def _download(
        self, torrent_id: str, stream: StreamRef
    ) -> tuple[dict[str, Any], int] | None:
        """Select the files and wait for the download. None when it is not ready in time."""
        deadline = time.monotonic() + self._max_wait
        info = await self._wait(torrent_id, {"waiting_files_selection", "downloaded"}, deadline)
        if info is None:
            return None
        target = _pick_file(_files(info), stream)
        if target is None:
            raise _DebridError("the file is not in the torrent")
        if info["status"] == "waiting_files_selection":
            ids = {file["id"] for file in _files(info) if _is_video(file)} | {target["id"]}
            await self._send(
                "select files",
                "POST",
                f"/torrents/selectFiles/{torrent_id}",
                data={"files": ",".join(str(i) for i in sorted(ids))},
            )
            info = await self._wait(torrent_id, {"downloaded"}, deadline)
            if info is None:
                return None
        return info, target["id"]

    async def _wait(
        self, torrent_id: str, statuses: set[str], deadline: float
    ) -> dict[str, Any] | None:
        while True:
            info = await self._info(torrent_id)
            status = info.get("status")
            if status in FAILED_STATUSES:
                raise _DebridError(f"Real-Debrid reports the torrent as {status}")
            if status in statuses:
                return info
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._poll_interval)

    async def _info(self, torrent_id: str) -> dict[str, Any]:
        info = await self._send("read torrent", "GET", f"/torrents/info/{torrent_id}")
        if not isinstance(info, dict) or not isinstance(info.get("status"), str):
            raise _unexpected()
        _files(info)
        return info

    async def _unrestrict(self, link: str) -> str:
        answer = await self._send(
            "unrestrict link", "POST", "/unrestrict/link", data={"link": link}
        )
        url = answer.get("download") if isinstance(answer, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            raise _DebridError("Real-Debrid gave no HTTPS download link")
        return url

    async def _discard(self, torrent_id: str) -> None:
        """Remove a torrent CueSync added. Best effort."""
        try:
            await self._send("delete torrent", "DELETE", f"/torrents/delete/{torrent_id}")
        except _DebridError as exc:
            log.debug("Real-Debrid could not delete the torrent it added: %s", exc)

    async def _send(self, what: str, method: str, path: str, **kwargs: Any) -> Any:
        """Call the API and return the JSON body, or None for an empty one (HTTP 204)."""
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
            raise _DebridError(f"Real-Debrid timed out ({what})") from None
        except httpx.HTTPError as exc:
            raise _DebridError(
                f"Real-Debrid request failed ({what}, {type(exc).__name__})"
            ) from None
        code = response.status_code
        if code == 401:
            raise _DebridError("Real-Debrid rejected the API key (HTTP 401)")
        if code == 403:
            raise _DebridError(
                "Real-Debrid refused access (HTTP 403), the account may be locked or not premium"
            )
        if code == 429:
            raise _DebridError("Real-Debrid rate limit (HTTP 429)")
        if response.is_error:
            raise _DebridError(f"Real-Debrid returned HTTP {code}")
        if code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise _DebridError(f"Real-Debrid sent an answer that is not JSON ({what})") from None


def _unexpected() -> _DebridError:
    return _DebridError("Real-Debrid sent an unexpected answer")


def _files(info: dict[str, Any]) -> list[dict[str, Any]]:
    """The `files` of a torrent info, checked for the fields used here."""
    files = info.get("files", [])
    if not isinstance(files, list):
        raise _unexpected()
    for file in files:
        if (
            not isinstance(file, dict)
            or not isinstance(file.get("id"), int)
            or not isinstance(file.get("path"), str)
            or not isinstance(file.get("bytes"), int)
        ):
            raise _unexpected()
    return files


def _basename(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name.casefold()


def _is_video(file: dict[str, Any]) -> bool:
    return PurePosixPath(file["path"].replace("\\", "/")).suffix.lower() in VIDEO_EXTENSIONS


def _pick_file(
    files: list[dict[str, Any]], stream: StreamRef, *, warn: bool = True
) -> dict[str, Any] | None:
    """The file of the torrent that is the played one: by name, then index, then size."""
    target = _find_file(files, stream)
    if target is None and warn:
        log.warning("Real-Debrid torrent %s has no file that matches the stream", stream.info_hash)
    return target


def _find_file(files: list[dict[str, Any]], stream: StreamRef) -> dict[str, Any] | None:
    size = stream.size
    if stream.filename:
        wanted = _basename(stream.filename)
        named = [
            file
            for file in files
            if _basename(file["path"]) == wanted and (size is None or file["bytes"] == size)
        ]
        if len(named) == 1:
            return named[0]
    if stream.file_idx is not None:
        for file in files:
            if file["id"] == stream.file_idx + 1:
                if size is not None and file["bytes"] != size:
                    break
                return file
    if size is not None:
        sized = [file for file in files if file["bytes"] == size]
        if len(sized) == 1:
            return sized[0]
    if stream.file_idx is None and files:
        return max(files, key=lambda file: file["bytes"])
    return None


def _link_for(info: dict[str, Any], target_id: int) -> str:
    """The link of the target file. The links follow the selected files in id order."""
    selected = sorted(file["id"] for file in _files(info) if file.get("selected") == 1)
    links = info.get("links")
    if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
        raise _unexpected()
    if target_id not in selected:
        raise _DebridError("the file is not selected in the torrent")
    if len(links) != len(selected):
        raise _DebridError("Real-Debrid packed the files into an archive, so there is no file link")
    return links[selected.index(target_id)]
