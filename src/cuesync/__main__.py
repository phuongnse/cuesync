"""Run the addon server: `python -m cuesync` or `cuesync`."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn

LOG_NAME = "cuesync.log"


def redirect_missing_streams() -> None:
    """Send output to a log file in the data directory when there is no console.

    `pythonw.exe` (the Windows autostart task) sets `sys.stdout` and `sys.stderr` to None.
    uvicorn's log setup would crash on that, and nobody would see the error.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    data_dir = Path(os.environ.get("CUESYNC_DATA_DIR") or "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    log = open(data_dir / LOG_NAME, "w", encoding="utf-8", buffering=1)  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log


def main() -> None:
    redirect_missing_streams()
    host = os.environ.get("CUESYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("CUESYNC_PORT", "7000"))
    uvicorn.run("cuesync.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
