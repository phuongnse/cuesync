"""SubDL API source: search by IMDb id and language, download as parsed cues."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from cuesync import __version__
from cuesync.config import UserConfig
from cuesync.sources.base import Candidate, SourceError, SubtitleRequest
from cuesync.subtitles import Subtitle, SubtitleError, parse_bytes, pick_from_zip

API_URL = "https://api.subdl.com/api/v1/subtitles"
DOWNLOAD_URL = "https://dl.subdl.com"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
IMDB_RE = re.compile(r"^tt\d+$")
ZIP_PATH_RE = re.compile(r"^/subtitle/[A-Za-z0-9._-]+\.zip$")
SOURCE_ID_RE = re.compile(r"(/subtitle/[A-Za-z0-9._-]+\.zip)(?::(\d+):(\d+))?")

log = logging.getLogger(__name__)
# httpx logs every request URL at INFO, and the API key is in the query string.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ISO 639-2 codes (B and T forms joined by "/") to the codes SubDL uses.
_CODES = {
    "eng": "EN",
    "fre/fra": "FR",
    "ger/deu": "DE",
    "spa": "ES",
    "ita": "IT",
    "por": "PT",
    "pob": "BR_PT",
    "chi/zho": "ZH",
    "zht": "ZH_BG",
    "dut/nld": "NL",
    "cze/ces": "CS",
    "gre/ell": "EL",
    "per/fas": "FA",
    "rum/ron": "RO",
    "slo/slk": "SK",
    "alb/sqi": "SQ",
    "mac/mkd": "MK",
    "may/msa": "MS",
    "nor/nob": "NO",
    "vie": "VI",
    "ara": "AR",
    "heb": "HE",
    "tur": "TR",
    "rus": "RU",
    "ukr": "UK",
    "pol": "PL",
    "swe": "SV",
    "dan": "DA",
    "fin": "FI",
    "hun": "HU",
    "bul": "BG",
    "hrv": "HR",
    "srp": "SR",
    "slv": "SL",
    "est": "ET",
    "lav": "LV",
    "lit": "LT",
    "jpn": "JA",
    "kor": "KO",
    "tha": "TH",
    "hin": "HI",
    "ind": "ID",
    "cat": "CA",
    "ben": "BN",
    "bos": "BS",
    "ice/isl": "IS",
    "geo/kat": "KA",
    "tam": "TA",
    "tel": "TE",
    "urd": "UR",
    "mal": "ML",
    "sin": "SI",
    "tgl": "TL",
    "bur/mya": "MY",
    "aze": "AZ",
    "bel": "BE",
    "kur": "KU",
}
_LANGUAGES: dict[str, str] = {
    code: subdl_code for codes, subdl_code in _CODES.items() for code in codes.split("/")
}


def _fail(what: str, failure: httpx.Response | Exception) -> SourceError:
    """Log a warning with a plain reason and return it as an error.

    The reason never holds headers, URLs or the API key.
    """
    if isinstance(failure, httpx.Response):
        code = failure.status_code
        if code in (401, 403):
            reason = f"SubDL rejected the API key (HTTP {code})"
        elif code == 429:
            reason = f"SubDL rate limit (HTTP {code})"
        else:
            reason = f"SubDL returned HTTP {code}"
    elif isinstance(failure, httpx.TimeoutException):
        reason = "SubDL request timed out"
    elif isinstance(failure, httpx.HTTPError):
        reason = f"SubDL request failed ({type(failure).__name__})"
    else:
        reason = "SubDL sent an unexpected answer"
    log.warning("SubDL %s failed: %s", what, reason)
    return SourceError(reason)


def _json(what: str, response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise _fail(what, exc) from None
    if not isinstance(body, dict):
        raise _fail(what, ValueError())
    return body


class SubDLSource:
    name = "subdl"

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._client = client

    @classmethod
    def from_config(cls, config: UserConfig, client: httpx.AsyncClient) -> SubDLSource | None:
        if not config.subdl_api_key:
            return None
        return cls(config.subdl_api_key, client)

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
        if not IMDB_RE.match(request.imdb_id):
            log.debug("SubDL search skipped: not an IMDb id")
            return []

        reverse: dict[str, str] = {}
        for code in request.languages:
            subdl_code = _LANGUAGES.get(code)
            if subdl_code is None:
                log.debug("SubDL has no language code for %r, skipping it", code)
                continue
            reverse.setdefault(subdl_code, code)
        if not reverse:
            return []

        params = {
            "api_key": self._api_key,
            "imdb_id": request.imdb_id,
            "languages": ",".join(sorted(reverse)),
            "subs_per_page": "30",
            "type": "movie",
        }
        if request.season is not None and request.episode is not None:
            params["type"] = "tv"
            params["season_number"] = str(request.season)
            params["episode_number"] = str(request.episode)

        try:
            response = await self._send("search", "GET", API_URL, params=sorted(params.items()))
            body = _json("search", response)
        except SourceError:
            return []

        if body.get("status") is not True:
            error = body.get("error")
            if isinstance(error, str) and "can't find" in error.lower():
                log.debug("SubDL has no subtitles for this title: %s", error)
            else:
                log.warning("SubDL search failed: SubDL reported an error")
            return []

        return _candidates(body, reverse, request.season, request.episode)

    async def download(self, candidate: Candidate) -> Subtitle:
        match = SOURCE_ID_RE.fullmatch(candidate.source_id)
        if match is None:
            raise SourceError("SubDL subtitle id is not valid")
        path, season, episode = match.groups()

        response = await self._send(
            "download",
            "GET",
            f"{DOWNLOAD_URL}{path}",
            headers={"User-Agent": f"CueSync v{__version__}"},
            follow_redirects=True,
        )
        picked = pick_from_zip(
            response.content,
            None,
            int(season) if season is not None else None,
            int(episode) if episode is not None else None,
        )
        if picked is None:
            raise SourceError("SubDL zip holds no usable subtitle")
        _, data = picked
        try:
            cues = parse_bytes(data, candidate.language)
        except SubtitleError:
            raise SourceError("SubDL file is not a usable subtitle") from None
        return Subtitle(
            cues=tuple(cues),
            language=candidate.language,
            source=self.name,
            source_id=candidate.source_id,
            release_name=candidate.release_name,
        )


def _candidates(
    body: dict[str, Any], reverse: dict[str, str], season: int | None, episode: int | None
) -> list[Candidate]:
    candidates: list[Candidate] = []
    seen: set[str] = set()
    items = body.get("subtitles")
    for item in items if isinstance(items, list) else []:
        try:
            language = reverse.get(str(item["language"]).upper())
            url = item["url"]
            if language is None or not ZIP_PATH_RE.fullmatch(url):
                continue
            source_id = url
            if season is not None and episode is not None:
                source_id = f"{url}:{season}:{episode}"
            if source_id in seen:
                continue
            seen.add(source_id)
            candidates.append(
                Candidate(
                    source="subdl",
                    source_id=source_id,
                    language=language,
                    release_name=str(item.get("release_name") or item.get("name") or ""),
                )
            )
        except (KeyError, TypeError, AttributeError):
            continue
    return candidates
