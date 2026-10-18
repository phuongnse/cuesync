# Running CueSync

How to run CueSync on your own PC and install it in Stremio Web and Stremio on Android.
Both need the addon over HTTPS. Stremio Web and the Android app refuse plain `http://`
addons, except on `127.0.0.1`.

## Prerequisites

- Python 3.12 or newer.
- ffmpeg (it includes ffprobe).

On Windows:

```powershell
winget install Python.Python.3.12
winget install Gyan.FFmpeg
```

Open a new terminal afterwards so the new programs are on `PATH`.

## Install

Get the source, then run the steps for your system from its folder.

On Linux (the script installs ffmpeg when it is missing):

```sh
scripts/setup.sh
.venv/bin/cuesync
```

On Windows:

```powershell
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\cuesync
```

The server listens on `http://127.0.0.1:7000`. Check it at `http://127.0.0.1:7000/health`.

## Settings

Set these in the environment before you start CueSync.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CUESYNC_HOST` | `127.0.0.1` | Address the server listens on. |
| `CUESYNC_PORT` | `7000` | Port the server listens on. |
| `CUESYNC_PUBLIC_URL` | from the request | The HTTPS address that Stremio uses to reach CueSync. |
| `CUESYNC_TORRENTIO_URL` | `https://torrentio.strem.fun` | A Torrentio-compatible server that CueSync asks for the torrent behind the played file. It may include a config path. |

Linux:

```sh
export CUESYNC_PUBLIC_URL=https://pc.tailnet.ts.net
.venv/bin/cuesync
```

Windows (PowerShell):

```powershell
$env:CUESYNC_PUBLIC_URL = "https://pc.tailnet.ts.net"
.venv\Scripts\cuesync
```

Keep `CUESYNC_HOST` at `127.0.0.1`. The tunnel connects from the same PC, so nothing else
on your network needs to reach the server.

Set `CUESYNC_PUBLIC_URL` whenever you use a tunnel. The tunnel hands requests to CueSync
over local `http://`, so the address CueSync sees is not the one Stremio can use. The
install link on `/configure` is built from this setting. The value must start with
`http://` or `https://`. CueSync refuses to start otherwise. A trailing `/` is ignored.

## Tailscale Funnel

Funnel gives your PC a stable public HTTPS address of the form
`https://<machine>.<tailnet>.ts.net`. It does not change when you restart.

1. Install [Tailscale](https://tailscale.com/download) and log in.
2. In the Tailscale admin console, turn on HTTPS certificates for your tailnet and allow
   Funnel for it.
3. Start CueSync, then expose its port:

   ```sh
   tailscale funnel --bg 7000
   ```

   `tailscale funnel status` prints the public address.
4. Set `CUESYNC_PUBLIC_URL` to that address and restart CueSync.

## Cloudflare quick tunnel

A quick tunnel needs no account:

```sh
cloudflared tunnel --url http://localhost:7000
```

It prints a `https://<random>.trycloudflare.com` address. The address changes every time
the tunnel restarts. After a restart, set `CUESYNC_PUBLIC_URL` to the new address,
restart CueSync and install the addon again. Use Tailscale Funnel if you want one
address that stays the same.

## Install in Stremio

1. Open `<public URL>/configure` in a browser.
2. Fill in the form and create the install link.
3. On Stremio Web (<https://web.stremio.com>), paste the manifest URL into the addon
   search and install it.
4. On Android, sign in with the same Stremio account. Addons sync across devices. You can
   also paste the manifest URL into the addon search in the app.

The install URL contains your API keys. Keep it private and do not share it.
