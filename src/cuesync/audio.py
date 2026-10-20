"""Audio windows from a remote file: probe it, pick a track and decode with ffmpeg.

Each window is read by seeking in the remote file, so only a small part of it is
transferred. The URL is a secret (a debrid link), so it never appears in errors or logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass

SAMPLE_RATE = 16000
EDGE_S = 60.0
MAX_CONCURRENT = 3
TIMEOUT_S = 120.0
PROBE_TIMEOUT_S = 30.0
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Files are tagged with either ISO 639-2 spelling.
_ALIASES = {
    "fre": "fra",
    "ger": "deu",
    "chi": "zho",
    "zht": "zho",
    "dut": "nld",
    "cze": "ces",
    "gre": "ell",
    "per": "fas",
    "rum": "ron",
    "slo": "slk",
    "alb": "sqi",
    "mac": "mkd",
    "may": "msa",
    "baq": "eus",
    "arm": "hye",
    "geo": "kat",
    "ice": "isl",
    "wel": "cym",
    "bur": "mya",
    "tib": "bod",
    "pob": "por",
    "nob": "nor",
}

log = logging.getLogger(__name__)


class AudioError(Exception):
    """Probing or extraction failed. The message never holds the file URL."""


@dataclass(frozen=True)
class AudioTrack:
    index: int  # counts audio streams only, so it maps to `-map 0:a:{index}`
    language: str | None
    default: bool


@dataclass(frozen=True)
class MediaInfo:
    duration_s: float
    audio_tracks: tuple[AudioTrack, ...]


@dataclass(frozen=True)
class AudioWindow:
    start_s: float
    samples: bytes  # mono 16 kHz signed 16-bit little-endian PCM

    @property
    def duration_s(self) -> float:
        return len(self.samples) / 2 / SAMPLE_RATE


async def _run(args: list[str], timeout: float) -> bytes:
    """Run a process and return its stdout. It is killed on timeout and on cancellation.

    The messages carry no stderr: it can echo the URL, or the CDN URL it redirects to.
    """
    name = os.path.basename(args[0])
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise AudioError(f"{name} is not installed") from None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        raise AudioError(f"{name} timed out after {timeout:.0f} s") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        raise AudioError(f"{name} failed (exit {proc.returncode})")
    return stdout


async def probe(url: str, timeout: float = PROBE_TIMEOUT_S) -> MediaInfo:
    """The duration and the audio tracks of a remote file."""
    args = [
        FFPROBE,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "format=duration:stream=index:stream_tags=language:stream_disposition=default",
        "-of",
        "json",
        url,
    ]
    out = await _run(args, timeout)
    try:
        data = json.loads(out)
        duration = float(data["format"]["duration"])
        streams = data.get("streams") or []
    except (ValueError, KeyError, TypeError):
        raise AudioError("ffprobe returned no usable duration") from None
    if not duration > 0 or duration == float("inf"):
        raise AudioError("the file has no usable duration")
    tracks = []
    for position, stream in enumerate(streams):
        language = str((stream.get("tags") or {}).get("language") or "").lower()
        default = bool((stream.get("disposition") or {}).get("default"))
        tracks.append(
            AudioTrack(position, language if language not in ("", "und") else None, default)
        )
    return MediaInfo(duration, tuple(tracks))


def _norm(code: str) -> str:
    return _ALIASES.get(code, code)


def select_audio_track(tracks: Sequence[AudioTrack], language: str | None) -> int:
    """The track in the subtitle language, else the default track, else the first one."""
    if not tracks:
        raise AudioError("the file has no audio track")
    if language:
        wanted = _norm(language.lower())
        matches = [t for t in tracks if t.language and _norm(t.language) == wanted]
        for track in matches:
            if track.default:
                return track.index
        if matches:
            return matches[0].index
    for track in tracks:
        if track.default:
            return track.index
    return tracks[0].index


def window_starts(
    duration_s: float, count: int, length_s: float, edge_s: float = EDGE_S
) -> list[float]:
    """Start times of evenly spaced, non-overlapping windows that skip the file's edges."""
    if duration_s <= 0 or count < 1:
        return []
    length = min(length_s, duration_s)
    lo, hi = edge_s, duration_s - edge_s
    if hi - lo < length:  # too short to skip the edges
        lo, hi = 0.0, duration_s
    span = hi - lo
    n = max(1, min(count, int(span // length)))
    gap = (span - n * length) / (n + 1)
    return [round(lo + gap + i * (length + gap), 3) for i in range(n)]


async def extract_windows(
    url: str,
    duration: float,
    count: int = 5,
    length_s: float = 120.0,
    *,
    track: int = 0,
    edge_s: float = EDGE_S,
    max_concurrent: int = MAX_CONCURRENT,
    timeout_s: float = TIMEOUT_S,
) -> list[AudioWindow]:
    """Decode evenly spaced windows to mono 16 kHz PCM, a few ffmpeg processes at a time.

    A window that fails is dropped with a warning. Raises `AudioError` when all fail.
    """
    length = min(length_s, duration)
    starts = window_starts(duration, count, length_s, edge_s)
    semaphore = asyncio.Semaphore(max_concurrent)

    async def one(start: float) -> AudioWindow:
        args = [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.3f}",
            "-i",
            url,
            "-t",
            f"{length:.3f}",
            "-map",
            f"0:a:{track}",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]
        async with semaphore:
            return AudioWindow(start, await _run(args, timeout_s))

    results = await asyncio.gather(*(one(s) for s in starts), return_exceptions=True)
    windows = []
    for start, result in zip(starts, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            log.warning("audio window at %.0f s failed: %s", start, result)
        elif result.samples:
            windows.append(result)
    if not windows:
        raise AudioError("no audio window could be read")
    return sorted(windows, key=lambda w: w.start_s)
