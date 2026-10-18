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
| `CUESYNC_DATA_DIR` | `./data` | Where the cache database and its files live. |
| `CUESYNC_CACHE_MAX_MB` | `500` | Size limit of the stored subtitle and audio files. The least recently used files are deleted first. |

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

When you run CueSync directly, keep `CUESYNC_HOST` at `127.0.0.1`. The tunnel connects
from the same PC, so nothing else on your network needs to reach the server. The Docker
image sets `0.0.0.0` itself, see [Run with Docker](#run-with-docker).

Set `CUESYNC_PUBLIC_URL` whenever you use a tunnel. The tunnel hands requests to CueSync
over local `http://`, so the address CueSync sees is not the one Stremio can use. The
install link on `/configure` is built from this setting. The value must start with
`http://` or `https://`. CueSync refuses to start otherwise. A trailing `/` is ignored.

## Data directory

CueSync keeps its cache database and files in the data directory (`CUESYNC_DATA_DIR`).

- Run directly: `./data` under the folder you start CueSync in.
- Docker: `/data` in the `cuesync-data` volume.
- Windows task: `%LOCALAPPDATA%\CueSync`.

The data directory holds only cached subtitles, audio analysis and sync results. Deleting
it only clears the cache. CueSync rebuilds it as you watch.

## Run with Docker

The image holds Python, ffmpeg and CueSync. It runs as a non-root user and stores its
cache in the `/data` volume. `compose.yaml` runs it next to a Tailscale container that
publishes it over HTTPS with Funnel. Both containers start again after a reboot.

Prerequisites:

- Docker with Compose v2.
- A Tailscale [auth key](https://login.tailscale.com/admin/settings/keys).
- HTTPS certificates and Funnel turned on for your tailnet, as in
  [Tailscale Funnel](#tailscale-funnel).

Steps:

1. Create a `.env` file next to `compose.yaml`:

   ```sh
   TS_AUTHKEY=tskey-auth-...
   CUESYNC_PUBLIC_URL=https://cuesync.<tailnet>.ts.net
   ```

   The Tailscale container uses the host name `cuesync`, so the address has that form.
2. Start both containers:

   ```sh
   docker compose up -d --build
   ```

3. Check them:

   ```sh
   docker compose ps
   docker compose logs
   ```

The containers share one network and Compose publishes no host port. Funnel reaches
CueSync on `127.0.0.1:7000` inside that network, and nothing else on your network can
reach it. `deploy/tailscale/serve.json` holds the Funnel setup, so you do not run
`tailscale funnel` yourself. Then open `<public URL>/configure` as in
[Install in Stremio](#install-in-stremio).

To run only the image, with your own tunnel:

```sh
docker build -t cuesync .
docker run -d --restart unless-stopped -p 127.0.0.1:7000:7000 -v cuesync-data:/data \
  -e CUESYNC_PUBLIC_URL=https://pc.tailnet.ts.net cuesync
```

The image sets `CUESYNC_HOST=0.0.0.0` so the published port reaches the server, and
`CUESYNC_DATA_DIR=/data`. The data lives in the `cuesync-data` volume.

## Start automatically on Windows

Task Scheduler starts CueSync when you log in. It is built into Windows, needs no extra
download and stores no password. The task starts `pythonw.exe`, so no console window
appears. Without a console, CueSync writes its log to `cuesync.log` in the data directory.

1. Install CueSync as in [Install](#install).
2. Store the settings as user environment variables, then open a new terminal. The data
   directory is an absolute path so the cache stays in one place, whatever folder the task
   starts in.

   ```powershell
   setx CUESYNC_PUBLIC_URL https://pc.tailnet.ts.net
   setx CUESYNC_DATA_DIR "$env:LOCALAPPDATA\CueSync"
   ```

3. Expose the port once. Tailscale runs as a Windows service, and this setting survives
   reboots:

   ```powershell
   tailscale funnel --bg 7000
   ```

4. In PowerShell, from the CueSync folder, register the task:

   ```powershell
   $action = New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\pythonw.exe" -Argument "-m cuesync" -WorkingDirectory $PWD
   $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
   $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
   Register-ScheduledTask -TaskName CueSync -Action $action -Trigger $trigger -Settings $settings
   ```

   The time limit is zero because Task Scheduler stops a task after three days otherwise.
5. Start it now and check `http://127.0.0.1:7000/health`:

   ```powershell
   Start-ScheduledTask CueSync
   ```

6. To stop or remove it:

   ```powershell
   Stop-ScheduledTask CueSync
   Unregister-ScheduledTask CueSync
   ```

A task that runs before anyone logs in needs your password stored in Task Scheduler, so
this setup starts at logon instead.

## Update

The cache survives updates because it lives in the data directory.

Docker:

```sh
git pull
docker compose up -d --build
```

Windows:

```powershell
Stop-ScheduledTask CueSync
git pull
.venv\Scripts\pip install -e .
Start-ScheduledTask CueSync
```

Linux:

```sh
git pull
scripts/setup.sh
```

Then restart `.venv/bin/cuesync`.

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
