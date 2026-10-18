"""The HTTP server that Stremio talks to.

Stremio's addon protocol is plain HTTP + JSON:
https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/protocol.md
"""

from __future__ import annotations

import html
import logging
import os
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from . import __version__
from .cache import DEFAULT_MAX_BYTES, Cache
from .config import InvalidConfig, RedactConfigFilter, UserConfig, decode_config
from .jobs import JOB_LIMIT, JOB_TIMEOUT, Job, Jobs, JobState
from .placeholder import placeholder_cues
from .retime import Stages, pick, retime
from .search import build_sources, decode_key, encode_key, gather_candidates, rank
from .sources.base import Candidate, SourceError, SubtitleRequest, SubtitleSource
from .stremio import Extra, VideoId, parse_extra, parse_video_id, raw_extra_segment
from .subtitles import write_srt
from .sync import Correction, Verdict, apply_correction

log = logging.getLogger(__name__)

MAX_HASH_LENGTH = 64

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


def cache_max_bytes(value: str | None) -> int:
    """The size limit of stored files in bytes, from `CUESYNC_CACHE_MAX_MB`."""
    if value is None or not value.strip():
        return DEFAULT_MAX_BYTES
    try:
        megabytes = int(value)
    except ValueError:
        megabytes = 0
    if megabytes <= 0:
        raise ValueError(f"CUESYNC_CACHE_MAX_MB must be a positive whole number, got {value!r}")
    return megabytes * 1024 * 1024


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
    cache: Cache = request.app.state.cache
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
        cache=cache,
    )
    ranked = rank(candidates, config.languages, extra.filename, config.max_per_language)
    base = f"{base_url(request)}/{request.path_params['config']}"
    entries = []
    video_hash = extra.video_hash
    if _can_sync(video_hash, extra):
        jobs: Jobs = request.app.state.jobs
        stages: Stages = request.app.state.stages
        http: httpx.AsyncClient = request.app.state.http
        listed: list[tuple[Candidate, str]] = []
        to_start: list[Candidate] = []
        for candidate in pick(ranked):
            key = encode_key(candidate)
            correction, job = _sync_state(cache, jobs, video_hash, key)
            if correction is not None:
                wanted = correction.verdict is Verdict.SYNCED
            elif job is not None:
                wanted = job.state in (JobState.PENDING, JobState.RUNNING)
            else:
                wanted = stages.debrid(config, http) is not None
                if wanted:
                    to_start.append(candidate)
            if wanted:
                listed.append((candidate, key))
        if to_start:

            async def work(cands: list[Candidate]) -> dict[Candidate, Correction]:
                return await retime(stages, sources, http, config, video, extra, cands, cache=cache)

            jobs.submit(video_hash, to_start, work)
        numbers: dict[str, int] = {}
        for candidate, key in listed:
            language = candidate.language
            numbers[language] = numbers.get(language, 0) + 1
            entries.append(
                {
                    "id": f"cuesync-{language}-{numbers[language]}",
                    "url": f"{base}/retimed/{video_hash}/{key}.srt",
                    "lang": language,
                }
            )
    entries.extend(
        {"id": key, "url": f"{base}/sub/{key}.srt", "lang": candidate.language}
        for candidate in ranked
        for key in (encode_key(candidate),)
    )
    return {"subtitles": entries}


def _valid_hash(video_hash: str | None) -> bool:
    """Jobs are keyed by the video hash and it is part of a URL path, so it must be plain."""
    return (
        bool(video_hash)
        and video_hash.isascii()
        and video_hash.isalnum()
        and len(video_hash) <= MAX_HASH_LENGTH
    )


def _can_sync(video_hash: str | None, extra: Extra) -> bool:
    return _valid_hash(video_hash) and bool(extra.filename or extra.video_size is not None)


def _sync_state(
    cache: Cache, jobs: Jobs, video_hash: str, key: str
) -> tuple[Correction | None, Job | None]:
    """The correction of a subtitle, from the cache or else from its finished job, and its job."""
    correction = cache.get_sync(video_hash, key)
    job = jobs.get(video_hash, key)
    if correction is None and job is not None and job.state is JobState.DONE:
        correction = job.correction
    return correction, job


async def _serve_srt(
    request: Request,
    config: UserConfig,
    source_name: str,
    source_id: str,
    language: str,
    correction: Correction | None = None,
) -> Response:
    """Serve a subtitle from the cache, or download it from its source and keep it.

    The cache holds the original. The correction, when there is one, is applied on each serve.
    """
    cache: Cache = request.app.state.cache
    candidate = Candidate(source_name, source_id, language, release_name="")
    cues = cache.get_subtitle(encode_key(candidate))
    if cues is None:
        sources = request.app.state.sources(config, request.app.state.http)
        source = next((s for s in sources if s.name == source_name), None)
        if source is None:
            raise HTTPException(404, "Unknown subtitle")
        try:
            subtitle = await source.download(candidate)
        except SourceError as exc:
            raise HTTPException(502, str(exc)) from None
        cues = subtitle.cues
        cache.put_subtitle(encode_key(candidate), cues)
    if correction:
        cues = apply_correction(cues, correction)
    return Response(write_srt(cues), media_type="application/x-subrip; charset=utf-8")


def create_app(
    public_url: str | None = None,
    source_factory: Callable[[UserConfig, httpx.AsyncClient], Sequence[SubtitleSource]]
    | None = None,
    stages: Stages | None = None,
    job_limit: int = JOB_LIMIT,
    job_timeout: float = JOB_TIMEOUT,
    cache: Cache | None = None,
) -> FastAPI:
    redact_access_log()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await app.state.jobs.close()
        await app.state.http.aclose()
        app.state.cache.close()

    app = FastAPI(title="CueSync", version=__version__, lifespan=lifespan)
    app.state.sources = source_factory or build_sources
    app.state.http = httpx.AsyncClient()
    app.state.stages = stages or Stages()
    app.state.jobs = Jobs(job_limit, job_timeout)
    if cache is None:
        cache = Cache(
            Path(os.environ.get("CUESYNC_DATA_DIR") or "data"),
            max_bytes=cache_max_bytes(os.environ.get("CUESYNC_CACHE_MAX_MB")),
        )
    app.state.cache = cache
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
        try:
            source_name, source_id, language = decode_key(key)
        except ValueError:
            raise HTTPException(404, "Unknown subtitle") from None
        return await _serve_srt(request, config, source_name, source_id, language)

    @app.get("/{config}/retimed/{video_hash}/{key}.srt")
    async def retimed_file(
        request: Request, video_hash: str, key: str, config: ConfigDep
    ) -> Response:
        try:
            source_name, source_id, language = decode_key(key)
        except ValueError:
            raise HTTPException(404, "Unknown subtitle") from None
        if not _valid_hash(video_hash):
            raise HTTPException(404, "Unknown subtitle")
        jobs: Jobs = request.app.state.jobs
        correction, job = _sync_state(request.app.state.cache, jobs, video_hash, key)
        if (
            correction is None
            and job is not None
            and job.state
            in (
                JobState.PENDING,
                JobState.RUNNING,
            )
        ):
            # No-store, so the player asks again and gets the synced file once it is ready.
            return Response(
                write_srt(placeholder_cues(language)),
                media_type="application/x-subrip; charset=utf-8",
                headers={"Cache-Control": "no-store"},
            )
        if correction is not None and correction.verdict is not Verdict.SYNCED:
            correction = None
        return await _serve_srt(request, config, source_name, source_id, language, correction)

    @app.get("/{config}/status/{video_hash}.json")
    def status(request: Request, video_hash: str, config: ConfigDep) -> dict:
        jobs: Jobs = request.app.state.jobs
        return {
            "videoHash": video_hash,
            "jobs": [
                {
                    "subtitle": job.subtitle_key,
                    "lang": job.language,
                    "state": job.state.value,
                    "verdict": job.correction.verdict.value if job.correction else None,
                    "scale": job.correction.scale if job.correction else None,
                    "offsetMs": job.correction.offset_ms if job.correction else None,
                    "error": job.error,
                }
                for job in jobs.for_video(video_hash)
            ],
        }

    return app


app = create_app()
