"""The HTTP server that Stremio talks to.

Stremio's addon protocol is plain HTTP + JSON:
https://github.com/Stremio/stremio-addon-sdk/blob/master/docs/protocol.md
"""

from __future__ import annotations

from urllib.parse import parse_qsl

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__

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


def parse_extra(extra: str) -> dict[str, str]:
    """Parse the `extra` path segment Stremio sends, such as
    `videoHash=8e24...&videoSize=1478453234&filename=Movie.2010.1080p.mkv`."""
    return dict(parse_qsl(extra, keep_blank_values=True))


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
        return {"subtitles": []}

    @app.get("/subtitles/{type}/{id}/{extra}.json")
    def subtitles_with_extra(type: str, id: str, extra: str) -> dict:
        parse_extra(extra)
        return {"subtitles": []}

    return app


app = create_app()
