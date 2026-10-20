"""Keep every test away from the real `./data` directory."""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

# `test_app.py` builds an app when it is imported, before any fixture runs.
os.environ["CUESYNC_DATA_DIR"] = tempfile.mkdtemp(prefix="cuesync-test-")


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CUESYNC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CUESYNC_CACHE_MAX_MB", raising=False)
    return tmp_path


@pytest.fixture(scope="session")
def mkv_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    """A 30 s mono mkv made with ffmpeg."""
    path = tmp_path_factory.mktemp("mkv") / "tone.mkv"
    source = "sine=frequency=440:sample_rate=8000"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source,
            "-t",
            "30",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()
