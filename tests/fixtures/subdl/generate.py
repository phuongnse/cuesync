"""Write pack.zip, a season pack in the shape SubDL serves, next to this script.

Run once with `.venv/bin/python tests/fixtures/subdl/generate.py` and commit the output.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

HERE = Path(__file__).parent
FIXED_TIME = (2024, 1, 1, 0, 0, 0)

MEMBERS = {
    "Show.S01E01.srt": "1\n00:00:01,000 --> 00:00:02,000\nFirst episode line\n",
    "Show.S01E02.srt": "1\n00:00:03,000 --> 00:00:04,500\nSecond episode line\n",
    "readme.txt": "Subtitles for Show, season 1.\n",
}


def main() -> None:
    with zipfile.ZipFile(HERE / "pack.zip", "w") as zf:
        for name, text in MEMBERS.items():
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text.encode("utf-8"))


if __name__ == "__main__":
    main()
