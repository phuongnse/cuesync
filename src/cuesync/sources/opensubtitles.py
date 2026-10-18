"""OpenSubtitles REST API source: search by IMDb id and language, download as parsed cues."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from cuesync import __version__
from cuesync.config import UserConfig
from cuesync.sources.base import Candidate, SourceError, SubtitleRequest
from cuesync.subtitles import Subtitle, SubtitleError, parse_bytes

API_URL = "https://api.opensubtitles.com/api/v1"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
TOKEN_LIFETIME = 12 * 60 * 60  # seconds
HASH_RE = re.compile(r"^[0-9a-f]{16}$")

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and the download link is a secret.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ISO 639-2 codes (B and T forms joined by "/") to the codes the API uses.
_CODES = {
    "eng": "en",
    "fre/fra": "fr",
    "ger/deu": "de",
    "spa": "es",
    "ita": "it",
    "por": "pt-pt",
    "pob": "pt-br",
    "chi/zho": "zh-cn",
    "zht": "zh-tw",
    "dut/nld": "nl",
    "cze/ces": "cs",
    "gre/ell": "el",
    "per/fas": "fa",
    "rum/ron": "ro",
    "slo/slk": "sk",
    "alb/sqi": "sq",
    "mac/mkd": "mk",
    "may/msa": "ms",
    "nor/nob": "no",
    "vie": "vi",
    "ara": "ar",
    "heb": "he",
    "tur": "tr",
    "rus": "ru",
    "ukr": "uk",
    "pol": "pl",
    "swe": "sv",
    "dan": "da",
    "fin": "fi",
    "hun": "hu",
    "bul": "bg",
    "hrv": "hr",
    "srp": "sr",
    "slv": "sl",
    "est": "et",
    "lav": "lv",
    "lit": "lt",
    "jpn": "ja",
    "kor": "ko",
    "tha": "th",
    "hin": "hi",
    "ind": "id",
    "cat": "ca",
    "baq/eus": "eu",
}
_LANGUAGES: dict[str, str] = {
    code: api_code for codes, api_code in _CODES.items() for code in codes.split("/")
}

# (api_key, username, password) -> (token, base API url, expires_at on the monotonic clock)
_tokens: dict[tuple[str, str, str], tuple[str, str, float]] = {}


def clear_tokens() -> None:
    _tokens.clear()


class _HttpFailure(SourceError):
    def __init__(self, reason: str, status_code: int) -> None:
        super().__init__(reason)
        self.status_code = status_code


def _fail(what: str, failure: httpx.Response | Exception) -> SourceError:
    """Log a warning with a plain reason and return it as an error.

    The reason never holds headers, URLs, tokens or credentials.
    """
    if isinstance(failure, httpx.Response):
        code = failure.status_code
        if code == 401:
            reason = f"OpenSubtitles rejected the API key or login (HTTP {code})"
        elif code == 406:
            reason = f"OpenSubtitles download quota used up (HTTP {code})"
            reset = _reset_time(failure)
            if reset:
                reason += f", resets in {reset}"
        elif code == 429:
            reason = f"OpenSubtitles rate limit (HTTP {code})"
        else:
            reason = f"OpenSubtitles returned HTTP {code}"
    elif isinstance(failure, httpx.TimeoutException):
        reason = "OpenSubtitles request timed out"
    elif isinstance(failure, httpx.HTTPError):
        reason = f"OpenSubtitles request failed ({type(failure).__name__})"
    else:
        reason = "OpenSubtitles sent an unexpected answer"
    log.warning("OpenSubtitles %s failed: %s", what, reason)
    if isinstance(failure, httpx.Response):
        return _HttpFailure(reason, failure.status_code)
    return SourceError(reason)


def _reset_time(response: httpx.Response) -> str | None:
    try:
        reset = response.json().get("reset_time")
    except (ValueError, AttributeError):
        return None
    return reset if isinstance(reset, str) and reset else None


def _json(what: str, response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise _fail(what, exc) from None
    if not isinstance(body, dict):
        raise _fail(what, ValueError())
    return body


class OpenSubtitlesSource:
    name = "opensubtitles"

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._username = username
        self._password = password

    @classmethod
    def from_config(
        cls, config: UserConfig, client: httpx.AsyncClient
    ) -> OpenSubtitlesSource | None:
        if not config.opensubtitles_api_key:
            return None
        return cls(
            config.opensubtitles_api_key,
            client,
            config.opensubtitles_username,
            config.opensubtitles_password,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Api-Key": self._api_key,
            "User-Agent": f"CueSync v{__version__}",
            "Accept": "application/json",
        }

    async def _send(self, what: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send a request. Any failure is logged and raised as SourceError."""
        try:
            response = await self._client.request(method, url, timeout=TIMEOUT, **kwargs)
        except httpx.HTTPError as exc:
            raise _fail(what, exc) from None
        if response.is_error:
            raise _fail(what, response)
        return response

    async def search(self, request: SubtitleRequest) -> list[Candidate]:
        reverse: dict[str, str] = {}
        for code in request.languages:
            api_code = _LANGUAGES.get(code)
            if api_code is None:
                log.debug("OpenSubtitles has no language code for %r, skipping it", code)
                continue
            reverse.setdefault(api_code, code)
        if not reverse:
            return []

        params = {"languages": ",".join(sorted(reverse))}
        try:
            imdb_number = str(int(request.imdb_id[2:]))
        except ValueError:
            log.debug("OpenSubtitles search skipped: not an IMDb id")
            return []
        if request.season is not None and request.episode is not None:
            params["parent_imdb_id"] = imdb_number
            params["season_number"] = str(request.season)
            params["episode_number"] = str(request.episode)
        else:
            params["imdb_id"] = imdb_number
        video_hash = (request.video_hash or "").lower()
        if HASH_RE.match(video_hash):
            params["moviehash"] = video_hash

        try:
            response = await self._send(
                "search",
                "GET",
                f"{API_URL}/subtitles",
                params=sorted(params.items()),
                headers=self._headers(),
            )
            body = _json("search", response)
        except SourceError:
            return []

        candidates = _candidates(body, reverse)
        candidates.sort(key=lambda candidate: not candidate.hash_match)
        return candidates

    async def download(self, candidate: Candidate) -> Subtitle:
        try:
            file_id = int(candidate.source_id)
        except ValueError:
            raise SourceError("OpenSubtitles file id is not a number") from None

        headers = self._headers()
        base_url = API_URL
        token_key = None
        if self._username and self._password:
            token_key = (self._api_key, self._username, self._password)
            login = await self._login(token_key)
            if login is not None:
                token, base_url = login
                headers["Authorization"] = f"Bearer {token}"

        try:
            response = await self._send(
                "download",
                "POST",
                f"{base_url}/download",
                json={"file_id": file_id},
                headers=headers,
            )
        except _HttpFailure as exc:
            if exc.status_code == 401 and "Authorization" in headers and token_key is not None:
                _tokens.pop(token_key, None)  # the token is stale, so log in again next time
            raise
        link = _json("download", response).get("link")
        if not isinstance(link, str) or not link:
            raise _fail("download", ValueError())

        file_response = await self._send(
            "file download",
            "GET",
            link,
            headers={"User-Agent": f"CueSync v{__version__}"},
            follow_redirects=True,
        )
        try:
            cues = parse_bytes(file_response.content, candidate.language)
        except SubtitleError:
            raise SourceError("OpenSubtitles file is not a usable subtitle") from None
        return Subtitle(
            cues=tuple(cues),
            language=candidate.language,
            source=self.name,
            source_id=candidate.source_id,
            release_name=candidate.release_name,
            hash_match=candidate.hash_match,
        )

    async def _login(self, key: tuple[str, str, str]) -> tuple[str, str] | None:
        """Return (token, base API url), or None when login fails and the download is anonymous."""
        cached = _tokens.get(key)
        if cached is not None and cached[2] > time.monotonic():
            return cached[0], cached[1]
        _tokens.pop(key, None)
        try:
            response = await self._send(
                "login",
                "POST",
                f"{API_URL}/login",
                json={"username": key[1], "password": key[2]},
                headers=self._headers(),
            )
            body = _json("login", response)
            token = body.get("token")
            if not isinstance(token, str) or not token:
                raise _fail("login", ValueError())
        except SourceError:
            log.warning("OpenSubtitles login failed, downloading without the account quota")
            return None
        host = body.get("base_url")
        base_url = f"https://{host}/api/v1" if isinstance(host, str) and host else API_URL
        _tokens[key] = (token, base_url, time.monotonic() + TOKEN_LIFETIME)
        return token, base_url


def _candidates(body: dict[str, Any], reverse: dict[str, str]) -> list[Candidate]:
    candidates: list[Candidate] = []
    data = body.get("data")
    for item in data if isinstance(data, list) else []:
        try:
            attributes = item["attributes"]
            language = reverse.get(str(attributes["language"]).lower())
            if language is None:
                continue
            hash_match = attributes.get("moviehash_match") is True
            for file in attributes["files"]:
                candidates.append(
                    Candidate(
                        source="opensubtitles",
                        source_id=str(file["file_id"]),
                        language=language,
                        release_name=attributes.get("release") or file.get("file_name") or "",
                        hash_match=hash_match,
                    )
                )
        except (KeyError, TypeError, AttributeError):
            continue
    return candidates
