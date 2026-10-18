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

## Install

1. Run the server and make it reachable over HTTPS (for example with Tailscale Funnel).
2. Open `https://<your-host>/configure`.
3. Fill in the form: your subtitle languages, your subtitle source keys and your debrid
   service.
4. Use the install link, or paste the manifest URL into Stremio's addon search.

The install URL contains your API keys. Keep it private and do not share it.

## Develop

Python 3.12+ and ffmpeg.

On Linux and macOS (the scripts are what CI and NexKit run):

```sh
scripts/setup.sh
scripts/lint.sh
scripts/test.sh
.venv/bin/cuesync                   # http://127.0.0.1:7000/configure
```

On Windows:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\ruff check . ; .venv\Scripts\ruff format --check .
.venv\Scripts\pytest -q
.venv\Scripts\cuesync
```

Development is driven by GitHub issues processed with [NexKit](https://github.com/phuongnse/nexkit);
see [docs/delivery.md](docs/delivery.md). [AGENTS.md](AGENTS.md) has the architecture and
conventions.

## License

MIT
