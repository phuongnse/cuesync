"""Run the addon server: `python -m cuesync` or `cuesync`."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("CUESYNC_HOST", "127.0.0.1")
    port = int(os.environ.get("CUESYNC_PORT", "7000"))
    uvicorn.run("cuesync.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
