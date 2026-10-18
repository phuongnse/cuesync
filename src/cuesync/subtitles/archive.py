"""Picking the right subtitle file out of a zip archive."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath

_EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa")
_MAX_MEMBER_SIZE = 5_000_000


def _stem(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).stem.lower()


def pick_from_zip(
    data: bytes,
    filename: str | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> tuple[str, bytes] | None:
    """Pick a subtitle from a zip and return its name and bytes, or None.

    Prefers the member named like `filename`, then the largest one for the episode,
    then the largest subtitle file. Other files, huge members and bad zips are ignored.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return None
    with zf:
        members = [
            info
            for info in zf.infolist()
            if not info.is_dir()
            and info.filename.lower().endswith(_EXTENSIONS)
            and not info.filename.startswith("__MACOSX/")
            and info.file_size <= _MAX_MEMBER_SIZE
        ]
        by_size = sorted(members, key=lambda info: info.file_size, reverse=True)
        ordered = []
        if filename:
            wanted = _stem(filename)
            ordered += [info for info in members if _stem(info.filename) == wanted]
        if season is not None and episode is not None:
            patterns = (
                re.compile(rf"s0*{season}[ ._-]*e0*{episode}(?!\d)", re.IGNORECASE),
                re.compile(rf"(?<!\d)0*{season}x0*{episode}(?!\d)", re.IGNORECASE),
            )
            ordered += [info for info in by_size if any(p.search(info.filename) for p in patterns)]
        ordered += by_size
        for info in ordered:
            try:
                return info.filename, zf.read(info)
            except (RuntimeError, zipfile.BadZipFile, NotImplementedError):
                continue
    return None
