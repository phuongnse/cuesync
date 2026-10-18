"""Pick the played file inside a torrent. Every debrid provider uses the same rule."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from cuesync.stream import StreamRef

VIDEO_EXTENSIONS = frozenset(
    {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg"}
)


@dataclass(frozen=True)
class TorrentFile:
    index: int  # 0-based position in the torrent, as Torrentio's fileIdx
    path: str
    size: int


def _basename(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).name.casefold()


def is_video(path: str) -> bool:
    return PurePosixPath(path.replace("\\", "/")).suffix.lower() in VIDEO_EXTENSIONS


def find_file(files: Sequence[TorrentFile], stream: StreamRef) -> TorrentFile | None:
    """The file of the torrent that is the played one: by name, then index, then size."""
    size = stream.size
    if stream.filename:
        wanted = _basename(stream.filename)
        named = [
            file
            for file in files
            if _basename(file.path) == wanted and (size is None or file.size == size)
        ]
        if len(named) == 1:
            return named[0]
    if stream.file_idx is not None:
        for file in files:
            if file.index == stream.file_idx:
                if size is not None and file.size != size:
                    break
                return file
    if size is not None:
        sized = [file for file in files if file.size == size]
        if len(sized) == 1:
            return sized[0]
    if stream.file_idx is None and files:
        return max(files, key=lambda file: file.size)
    return None
