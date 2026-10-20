"""The server starts as a real process with the environment the docs and the image use."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx

from cuesync.config import UserConfig, encode_config
from cuesync.search import encode_key
from cuesync.sources import Candidate

ROOT = Path(__file__).resolve().parents[1]
STARTUP_TIMEOUT = 20.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_server(env: dict[str, str]) -> Iterator[str]:
    """Start `python -m cuesync` and yield its base URL once `/health` answers."""
    base = f"http://127.0.0.1:{env['CUESYNC_PORT']}"
    process = subprocess.Popen(
        [sys.executable, "-m", "cuesync"],
        env={**os.environ, **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise AssertionError(f"server exited with {process.returncode}:\n{output}")
            try:
                httpx.get(f"{base}/health", timeout=1)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline:
                    raise AssertionError(f"no answer within {STARTUP_TIMEOUT}s") from None
                time.sleep(0.2)
        yield base
    finally:
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout:
            process.stdout.close()


def dockerfile_env() -> dict[str, str]:
    """The `CUESYNC_*` values of the Dockerfile's `ENV` lines."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("ENV "):
            values.update(re.findall(r"(CUESYNC_[A-Z_]+)=(\S+)", line))
    return values


def test_starts_with_documented_environment(tmp_path):
    data = tmp_path / "data"
    url = "https://cuesync.example.ts.net"
    env = {
        "CUESYNC_HOST": "127.0.0.1",
        "CUESYNC_PORT": str(free_port()),
        "CUESYNC_DATA_DIR": str(data),
        "CUESYNC_PUBLIC_URL": url,
    }
    with running_server(env) as base:
        health = httpx.get(f"{base}/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert url in httpx.get(f"{base}/configure").text
        # The cache opens on first use. A config without sources makes no network call.
        token = encode_config(UserConfig())
        key = encode_key(Candidate("subdl", "1", "vie", ""))
        assert httpx.get(f"{base}/{token}/sub/{key}.srt").status_code == 404
    assert (data / "files").is_dir()


def test_starts_with_the_dockerfile_environment(tmp_path):
    image_env = dockerfile_env()
    assert {"CUESYNC_HOST", "CUESYNC_PORT", "CUESYNC_DATA_DIR"} <= image_env.keys()
    env = {
        **image_env,
        "CUESYNC_PORT": str(free_port()),
        "CUESYNC_DATA_DIR": str(tmp_path / "data"),
    }
    with running_server(env) as base:
        assert httpx.get(f"{base}/health").status_code == 200


def test_deployment_variables_are_documented():
    names: set[str] = set()
    for name in ("Dockerfile", "compose.yaml"):
        names |= set(re.findall(r"CUESYNC_[A-Z_]+", (ROOT / name).read_text(encoding="utf-8")))
    assert names
    docs = (ROOT / "docs" / "running.md").read_text(encoding="utf-8")
    table = "\n".join(line for line in docs.splitlines() if line.startswith("|"))
    missing = sorted(name for name in names if f"`{name}`" not in table)
    assert not missing, f"not in the settings table of docs/running.md: {missing}"


def test_main_logs_to_the_data_directory_without_console_streams(tmp_path, monkeypatch):
    """`pythonw.exe` has no stdout or stderr. The server must still start and keep its log."""
    from cuesync import __main__ as entry

    data = tmp_path / "data"
    monkeypatch.setenv("CUESYNC_DATA_DIR", str(data))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    seen: dict[str, object] = {}

    def fake_run(app, **kwargs):
        seen["stdout"] = sys.stdout
        seen["stderr"] = sys.stderr
        print("serving")
        seen["app"] = app

    monkeypatch.setattr(entry.uvicorn, "run", fake_run)
    entry.main()
    log = seen["stdout"]
    assert log is not None and log is seen["stderr"]
    assert seen["app"] == "cuesync.app:app"
    log.close()
    assert (data / entry.LOG_NAME).read_text(encoding="utf-8") == "serving\n"


def test_main_keeps_existing_streams(tmp_path, monkeypatch):
    from cuesync import __main__ as entry

    monkeypatch.setenv("CUESYNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(entry.uvicorn, "run", lambda app, **kwargs: None)
    out, err = sys.stdout, sys.stderr
    entry.main()
    assert (sys.stdout, sys.stderr) == (out, err)
    assert not (tmp_path / "data").exists()
