# AGENTS.md

Guidance for coding agents (NexKit, Claude Code) working on CueSync.

## What CueSync is

A [Stremio](https://www.stremio.com/) subtitles addon. When a user plays a movie or
episode, Stremio asks the addon for subtitles. CueSync fetches Vietnamese
subtitles from SubDL, **re-times them to the audio of the exact file being
played**, and returns the re-timed files. No AI is involved: alignment uses voice activity
detection on the audio and the subtitle cue times (the approach of ffsubsync and alass).

It must work for Stremio Web and Stremio on Android, so it is served over HTTPS (the
user runs it on their own PC and exposes it with Tailscale Funnel or similar).

## How a request flows

1. Stremio calls `GET /{config}/subtitles/{type}/{id}/{extra}.json` (`{config}` is the
   base64url JSON of the user's settings). `id` is an IMDb id
   (`tt1375666`, or `tt0903747:1:2` for season 1 episode 2). `extra` carries `videoHash`
   (the video's hash), `videoSize` and `filename` of the file being played.
2. CueSync fetches candidate subtitles from SubDL, always in Vietnamese (`vie`), and
   normalizes them to UTF-8 SRT. At most five are listed.
3. It finds the torrent behind the played file (Torrentio lookup matched on filename and
   size) and gets a direct HTTPS link to that file from the user's Real-Debrid account. A
   user with no Real-Debrid key gets the original subtitles and no sync runs.
4. It extracts a few short audio windows spread over the file with ffmpeg (HTTP range
   reads; never the whole file).
5. It tries each frame-rate ratio (1, 25/23.976, 24/23.976, ...) by stretching the subtitle,
   estimates the offset in each window, and keeps the ratio `a` and shift `b` in
   `t' = a·t + b` that most windows agree on.
6. The subtitles list returns at once, without waiting for steps 3 to 5. A background job
   (one per video hash and subtitle, at most two at a time, five minutes each) runs them. Until
   it finishes, the re-timed entry serves a short "being synced, reopen the menu" subtitle.
   Afterwards the same URL serves the re-timed file, and the result is cached by video hash so
   the next request is instant. A failed job is remembered for ten minutes and its entry is
   left out. `/{config}/status/{videoHash}.json` lists the jobs for debugging.

## Layout

- `src/cuesync/app.py`: FastAPI app implementing the Stremio addon protocol.
- `src/cuesync/stremio.py`: parses the `extra` segment and the `id` of addon requests.
- `src/cuesync/__main__.py`: `python -m cuesync` / `cuesync` entry point (uvicorn).
- `src/cuesync/config.py`: user settings, their URL encoding and log redaction.
- `src/cuesync/subtitles/`: subtitle model, decoding, SRT/VTT/ASS parsing, SRT writing and zip picking.
- `src/cuesync/search.py`: concurrent source search, ranking and subtitle keys.
- `src/cuesync/audio.py`: probes a remote file, picks the audio track, and extracts mono 16 kHz audio windows with ffmpeg.
- `src/cuesync/sources/`: the `SubtitleSource` protocol and the subtitle source (SubDL).
- `src/cuesync/debrid/`: the `DebridProvider` protocol and the Real-Debrid client that turns a `StreamRef` into a direct HTTPS link.
- `src/cuesync/stream/`: the stream lookup that finds the torrent and file behind the played video (Torrentio).
- `src/cuesync/cache.py`: the SQLite plus files cache of searches, subtitles, speech timelines and sync results.
- `src/cuesync/retime.py`: runs stream lookup, Real-Debrid link, audio windows and sync for the top subtitles and returns the correction found for each.
- `src/cuesync/jobs.py`: the in-memory background sync jobs, deduplicated, limited in number and time.
- `src/cuesync/placeholder.py`: the "being synced" subtitle served while a job runs.
- `src/cuesync/sync/`: speech timelines at 100 Hz from audio (WebRTC VAD) and from subtitle cues, per-window subtitle offsets by FFT cross-correlation, and the global correction (frame-rate ratio and shift) with its verdict.
- `src/cuesync/configure.html`: the `/configure` page that builds the install URL.
- `Dockerfile`, `compose.yaml`, `deploy/`: the Docker image, the Compose example and the Tailscale serve config.
- `tests/`: pytest tests.

Add new modules under `src/cuesync/` by concern (for example `sources/`, `stream/`,
`audio.py`, `sync/`, `cache.py`) as the milestones introduce them.

## Commands

```sh
scripts/setup.sh             # ffmpeg (when missing) and the project into .venv
scripts/lint.sh              # ruff check and format check
scripts/test.sh              # pytest
.venv/bin/cuesync            # serves on 127.0.0.1:7000
```

CI and NexKit run exactly these scripts, so ffmpeg and ffprobe are always available in
tests there.

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
- `.github/` and `.nexkit/` are maintained by people; do not change them. A new system
  package goes in `scripts/setup.sh`, which CI and NexKit both run.
- A change that makes any doc statement false (README, AGENTS.md, `docs/`) fixes it in
  the same change. Docs state rules and reasons, not incident history or issue numbers.

## Roadmap

Work is tracked as GitHub issues grouped in milestones, processed with NexKit
(`/nexkit plan`, then `/nexkit go`). Each issue lists the issues it depends on. The
workflow is in [docs/delivery.md](docs/delivery.md).
