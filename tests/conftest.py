"""Keep every test away from the real `./data` directory."""

from __future__ import annotations

import os
import tempfile

import pytest

# `test_app.py` builds an app when it is imported, before any fixture runs.
os.environ["CUESYNC_DATA_DIR"] = tempfile.mkdtemp(prefix="cuesync-test-")


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CUESYNC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CUESYNC_CACHE_MAX_MB", raising=False)
    return tmp_path
