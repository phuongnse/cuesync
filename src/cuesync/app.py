"""The HTTP server that Stremio talks to.

Stremio's addon protocol is plain HTTP + JSON:
https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/protocol.md
"""

from __future__ import annotations

import logging
from importlib.resources import files
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from . import __version__
from .config import InvalidConfig, RedactConfigFilter, UserConfig, decode_config
from .stremio import VideoId, parse_extra, parse_video_id, raw_extra_segment

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


def configure_page() -> str:
    return files("cuesync").joinpath("configure.html").read_text(encoding="utf-8")


def redact_access_log() -> None:
    access_log = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactConfigFilter) for f in access_log.filters):
        access_log.addFilter(RedactConfigFilter())


def create_app() -> FastAPI:
    redact_access_log()
    app = FastAPI(title="CueSync", version=__version__)
    # Stremio Web and the apps fetch addon resources cross-origin.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/manifest.json")
    def manifest() -> dict:
        return {**MANIFEST, "behaviorHints": {"configurable": True, "configurationRequired": True}}

    @app.get("/configure", response_class=HTMLResponse)
    def configure() -> str:
        return configure_page()

    @app.get("/{config}/manifest.json")
    def configured_manifest(config: ConfigDep) -> dict:
        return {**MANIFEST, "behaviorHints": {"configurable": True}}

    @app.get("/{config}/configure", response_class=HTMLResponse)
    def reconfigure(config: str) -> str:
        # The page decodes the segment itself to pre-fill the form.
        return configure_page()

    @app.get("/{config}/subtitles/{type}/{id}.json")
    def subtitles(type: str, id: str, config: ConfigDep) -> dict:
        _parse_id_or_400(id)
        return {"subtitles": []}

    # `extra:path` because Starlette matches the decoded path: an encoded `/` in the
    # filename would otherwise end the segment early and give a 404.
    @app.get("/{config}/subtitles/{type}/{id}/{extra:path}.json")
    def subtitles_with_extra(
        request: Request, type: str, id: str, extra: str, config: ConfigDep
    ) -> dict:
        _parse_id_or_400(id)
        raw = request.scope.get("raw_path")
        parse_extra(raw_extra_segment(raw) if raw is not None else extra)
        return {"subtitles": []}

    return app


app = create_app()
