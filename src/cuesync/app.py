"""The HTTP server that Stremio talks to.

Stremio's addon protocol is plain HTTP + JSON:
https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/protocol.md
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
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


def create_app() -> FastAPI:
    app = FastAPI(title="CueSync", version=__version__)
    # Stremio Web and the apps fetch addon resources cross-origin.
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"])

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/manifest.json")
    def manifest() -> dict:
        return MANIFEST

    @app.get("/subtitles/{type}/{id}.json")
    def subtitles(type: str, id: str) -> dict:
        _parse_id_or_400(id)
        return {"subtitles": []}

    # `extra:path` because Starlette matches the decoded path: an encoded `/` in the
    # filename would otherwise end the segment early and give a 404.
    @app.get("/subtitles/{type}/{id}/{extra:path}.json")
    def subtitles_with_extra(request: Request, type: str, id: str, extra: str) -> dict:
        _parse_id_or_400(id)
        raw = request.scope.get("raw_path")
        parse_extra(raw_extra_segment(raw) if raw is not None else extra)
        return {"subtitles": []}

    return app


app = create_app()
