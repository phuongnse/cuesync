"""Piece math: which torrent pieces cover a byte range of one file."""

from __future__ import annotations

HEAD_BYTES = 2 * 1024 * 1024  # containers keep their headers at the start of the file
TAIL_BYTES = 2 * 1024 * 1024  # and often their index at the end
READ_AHEAD_BYTES = 8 * 1024 * 1024


def pieces_for_range(file_offset: int, piece_length: int, start: int, end: int) -> range:
    """The pieces that cover bytes `start` up to `end` (exclusive) of a file.

    `file_offset` is where the file starts inside the torrent. An empty range has no pieces.
    """
    if piece_length <= 0:
        raise ValueError("piece_length must be positive")
    if end <= start:
        return range(0)
    return range((file_offset + start) // piece_length, (file_offset + end - 1) // piece_length + 1)


def index_ranges(
    file_size: int, head: int = HEAD_BYTES, tail: int = TAIL_BYTES
) -> list[tuple[int, int]]:
    """The head and tail of a file as `(start, end)` ranges, merged when they overlap."""
    if file_size <= 0:
        return []
    head_end = min(head, file_size)
    tail_start = max(0, file_size - tail)
    if tail_start <= head_end:
        return [(0, file_size)]
    return [(0, head_end), (tail_start, file_size)]
