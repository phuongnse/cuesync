"""The HTTP server that Stremio talks to.

Stremio's addon protocol is plain HTTP + JSON:
https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/protocol.md
"""

from __future__ import annotations

import html
import logging
import os
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from . import __version__
from .config import InvalidConfig, RedactConfigFilter, UserConfig, decode_config
from .search import build_sources, decode_key, encode_key, gather_candidates, rank
from .sources.base import Candidate, SourceError, SubtitleRequest, SubtitleSource
from .stremio import Extra, VideoId, parse_extra, parse_video_id, raw_extra_segment
from .subtitles import write_srt

SRT_CACHE_SIZE = 100

MANIFEST = {
    "id": "community.cuesync",
    "version": __version__,
    "name": "CueSync",
    "description": "Re-times subtitles in any language to the movie's audio.",
    "resources": ["subtitles"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
}


def _parse_id_or_400(id: str) -> VideoId:
    try:
        return parse_video_id(id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid id") from None


def config_from_path(config: str) -> UserConfig:
    try:
        return decode_config(config)
    except InvalidConfig:
        raise HTTPException(
            400, "Invalid CueSync configuration. Open /configure to create a new install link."
        ) from None


ConfigDep = Annotated[UserConfig, Depends(config_from_path)]


def normalize_public_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not value.startswith(("http://", "https://")):
        raise ValueError(f"CUESYNC_PUBLIC_URL must start with http:// or https://, got {value!r}")
    return value.rstrip("/")


def base_url(request: Request) -> str:
    """The address clients use to reach this server. Every absolute URL is built from it."""
    return request.app.state.public_url or str(request.base_url).rstrip("/")


def configure_page(base: str) -> str:
    page = files("cuesync").joinpath("configure.html").read_text(encoding="utf-8")
    return page.replace("{{BASE_URL}}", html.escape(base, quote=True))


def redact_access_log() -> None:
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactConfigFilter) for f in access_log.filters):
        access_log.addFilter(RedactConfigFilter())


async def find_subtitles(
    request: Request, config: UserConfig, video: VideoId, extra: Extra
) -> dict:
    """Search the user's sources and list the best subtitles, each linked to its `.srt`."""
    sources = request.app.state.sources(config, request.app.state.http)
    candidates = await gather_candidates(
        sources,
        SubtitleRequest(
            imdb_id=video.imdb_id,
            season=video.season,
            episode=video.episode,
            languages=tuple(config.languages),
            video_hash=extra.video_hash,
            video_size=extra.video_size,
            filename=extra.filename,
        ),
    )
    ranked = rank(candidates, config.languages, extra.filename, config.max_per_language)
    prefix = f"{base_url(request)}/{request.path_params['config']}/sub"
    return {
        "subtitles": [
            {"id": key, "url": f"{prefix}/{key}.srt", "lang": candidate.language}
            for candidate in ranked
            for key in (encode_key(candidate),)
        ]
    }


def create_app(
    public_url: str | None = None,
    source_factory: Callable[[UserConfig, httpx.AsyncClient], Sequence[SubtitleSource]]
    | None = None,
) -> FastAPI:
    redact_access_log()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.http.aclose()

    app = FastAPI(title="CueSync", version=__version__, lifespan=lifespan)
    app.state.sources = source_factory or build_sources
    app.state.http = httpx.AsyncClient()
    app.state.srt_cache = OrderedDict()  # subtitle key -> SRT bytes, least recently used first
    app.state.public_url = normalize_public_url(
        public_url if public_url is not None else os.environ.get("CUESYNC_PUBLIC_URL")
    )
    # Stremio Web and the apps fetch addon resources cross-origin.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/manifest.json")
    def manifest() -> dict:
        return {**MANIFEST, "behaviorHints": {"configurable": True, "configurationRequired": True}}

    @app.get("/configure", response_class=HTMLResponse)
    def configure(request: Request) -> str:
        return configure_page(base_url(request))

    @app.get("/{config}/manifest.json")
    def configured_manifest(config: ConfigDep) -> dict:
        return {**MANIFEST, "behaviorHints": {"configurable": True}}

    @app.get("/{config}/configure", response_class=HTMLResponse)
    def reconfigure(request: Request, config: str) -> str:
        # The page decodes the segment itself to pre-fill the form.
        return configure_page(base_url(request))

    @app.get("/{config}/subtitles/{type}/{id}.json")
    async def subtitles(request: Request, type: str, id: str, config: ConfigDep) -> dict:
        video = _parse_id_or_400(id)
        return await find_subtitles(request, config, video, Extra(None, None, None))

    # `extra:path` because Starlette matches the decoded path: an encoded `/` in the
    # filename would otherwise end the segment early and give a 404.
    @app.get("/{config}/subtitles/{type}/{id}/{extra:path}.json")
    async def subtitles_with_extra(
        request: Request, type: str, id: str, extra: str, config: ConfigDep
    ) -> dict:
        video = _parse_id_or_400(id)
        raw = request.scope.get("raw_path")
        parsed = parse_extra(raw_extra_segment(raw) if raw is not None else extra)
        return await find_subtitles(request, config, video, parsed)

    @app.get("/{config}/sub/{key}.srt")
    async def subtitle_file(request: Request, key: str, config: ConfigDep) -> Response:
        cache: OrderedDict[str, bytes] = request.app.state.srt_cache
        body = cache.get(key)
        if body is None:
            try:
                source_name, source_id, language = decode_key(key)
            except ValueError:
                raise HTTPException(404, "Unknown subtitle") from None
            sources = request.app.state.sources(config, request.app.state.http)
            source = next((s for s in sources if s.name == source_name), None)
            if source is None:
                raise HTTPException(404, "Unknown subtitle")
            candidate = Candidate(source_name, source_id, language, release_name="")
            try:
                subtitle = await source.download(candidate)
            except SourceError as exc:
                raise HTTPException(502, str(exc)) from None
            body = write_srt(subtitle.cues)
            cache[key] = body
            while len(cache) > SRT_CACHE_SIZE:
                cache.popitem(last=False)
        else:
            cache.move_to_end(key)
        return Response(body, media_type="application/x-subrip; charset=utf-8")

    return app


app = create_app()
