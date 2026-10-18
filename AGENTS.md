# AGENTS.md

Guidance for coding agents (NexKit, Claude Code) working on CueSync.

## What CueSync is

A [Stremio](https://www.stremio.com/) subtitles addon. When a user plays a movie or
episode, Stremio asks the addon for subtitles. CueSync fetches subtitles in the user's
languages from public sources, **re-times them to the audio of the exact file being
played**, and returns the re-timed files. No AI is involved: alignment uses voice activity
detection on the audio and the subtitle cue times (the approach of ffsubsync and alass).

It must work for Stremio Web and Stremio on Android, so it is served over HTTPS (the
user runs it on their own PC and exposes it with Tailscale Funnel or similar).

## How a request flows

1. Stremio calls `GET /{config}/subtitles/{type}/{id}/{extra}.json` (the `{config}`
   prefix arrives with the configuration issue). `id` is an IMDb id
   (`tt1375666`, or `tt0903747:1:2` for season 1 episode 2). `extra` carries `videoHash`
   (OpenSubtitles hash), `videoSize` and `filename` of the file being played.
2. CueSync fetches candidate subtitles from the configured sources (OpenSubtitles, SubDL)
   and normalizes them to UTF-8 SRT.
3. It finds the torrent behind the played file (Torrentio lookup matched on filename and
   size) and gets a direct HTTPS link to that file from the user's debrid service.
4. It extracts a few short audio windows spread over the file with ffmpeg (HTTP range
   reads; never the whole file).
5. For each window it estimates the subtitle offset, then fits `t' = a·t + b` across
   windows, snapping `a` to known frame-rate ratios (25/23.976, 24/23.976, ...).
6. It returns re-timed subtitles next to the originals, and caches the result by video
   hash so the next request is instant.

## Layout

- `src/cuesync/app.py`: FastAPI app implementing the Stremio addon protocol.
- `src/cuesync/__main__.py`: `python -m cuesync` / `cuesync` entry point (uvicorn).
- `tests/`: pytest tests.

Add new modules under `src/cuesync/` by concern (for example `sources/`, `stream/`,
`audio.py`, `sync/`, `cache.py`) as the milestones introduce them.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/cuesync            # serves on 127.0.0.1:7000
```

ffmpeg and ffprobe are installed on CI and in NexKit runs.

## Conventions

- Python 3.12+, type hints, `from __future__ import annotations`.
- Ruff for lint and format (line length 100); fix what it reports rather than ignoring.
- Every behaviour change comes with tests. Tests never hit the network: fake HTTP with
  `httpx.MockTransport` or recorded fixtures under `tests/fixtures/`. Generate media for
  audio tests with ffmpeg (`lavfi` sources) instead of committing large files.
- Secrets (debrid keys, subtitle API keys) come from the user's addon config or the
  environment. Never log them, and never put them in error messages.
- Keep the addon responsive: Stremio gives up on slow subtitle responses, so long work
  runs in the background and the response returns what is ready.
- Add a dependency only when it earns its place, with a lower bound in `pyproject.toml`.
- `.github/` and `.nexkit/` are maintained by people; do not change them.

## Roadmap

Work is tracked as GitHub issues grouped in milestones, processed with NexKit
(`/nexkit plan`, then `/nexkit go`). Each issue lists the issues it depends on.
