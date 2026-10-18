# CueSync

A Stremio addon that re-times subtitles in any language to the audio of the movie you
are playing, so subtitles made for a different release stop drifting.

> Status: early development. See the [milestones](https://github.com/phuongnse/cuesync/milestones).

## How it works

CueSync fetches subtitles in your languages from OpenSubtitles and SubDL, reads a few
short audio windows of the exact file you play (through your debrid service), detects
where people speak and shifts and stretches the subtitles to match. It fixes constant
offsets and frame-rate drift (for example 25 fps subtitles on a 23.976 fps file). It does
not translate or generate subtitles.

## Develop

Python 3.12+ and ffmpeg.

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
.venv/bin/pytest -q
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/cuesync                   # http://127.0.0.1:7000/manifest.json
```

Development is driven by GitHub issues processed with [NexKit](https://github.com/phuongnse/nexkit).
See [AGENTS.md](AGENTS.md) for the architecture and conventions.

## License

MIT
