"""Re-time the best subtitle of each language to the audio of the played file.

The stages run in order: stream lookup, debrid link, audio windows and sync. Every stage is
injectable so tests can fake it. Any failure gives an empty result, so the caller falls back
to the original subtitles. The debrid link is a secret and is never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import httpx
import numpy as np

from cuesync import audio
from cuesync.audio import AudioError, AudioWindow, MediaInfo, select_audio_track
from cuesync.config import UserConfig
from cuesync.debrid import DebridProvider, debrid_provider
from cuesync.sources.base import Candidate, SubtitleSource
from cuesync.stream import StreamRef, find_stream
from cuesync.stremio import Extra, VideoId
from cuesync.subtitles import Cue, Subtitle, write_srt
from cuesync.sync import Correction, Verdict, align, apply_correction, audio_speech

# Stremio gives up on slow subtitle responses. This comes on top of the search budget.
RETIME_BUDGET = 15.0
# Each re-timed subtitle costs a download from the user's daily source quota.
RETIME_PER_LANGUAGE = 1

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stages:
    """The work `retime` does. The defaults are the real functions."""

    find_stream: Callable[
        [httpx.AsyncClient, VideoId, str | None, int | None], Awaitable[StreamRef | None]
    ] = find_stream
    debrid: Callable[[UserConfig, httpx.AsyncClient], DebridProvider | None] = debrid_provider
    probe: Callable[[str], Awaitable[MediaInfo]] = audio.probe
    extract_windows: Callable[..., Awaitable[list[AudioWindow]]] = audio.extract_windows
    align: Callable[[Sequence[tuple[float, np.ndarray]], Sequence[Cue]], Correction] = align


@dataclass(frozen=True)
class Retimed:
    candidate: Candidate
    correction: Correction
    original_srt: bytes
    srt: bytes


async def _prepare_audio(stages: Stages, url: str) -> list[tuple[float, np.ndarray]] | None:
    """Read the audio windows once and turn each into a speech timeline."""
    try:
        info = await stages.probe(url)
        track = select_audio_track(info.audio_tracks, None)
        windows = await stages.extract_windows(url, info.duration_s, track=track)
    except AudioError as exc:
        log.warning("could not read the audio: %s", exc)
        return None
    return await asyncio.to_thread(lambda: [(w.start_s, audio_speech(w.samples)) for w in windows])


async def _download_all(
    sources: Sequence[SubtitleSource], picked: Sequence[Candidate]
) -> list[tuple[Candidate, Subtitle]]:
    by_name = {source.name: source for source in sources}
    known = [candidate for candidate in picked if candidate.source in by_name]
    results = await asyncio.gather(
        *(by_name[candidate.source].download(candidate) for candidate in known),
        return_exceptions=True,
    )
    downloaded: list[tuple[Candidate, Subtitle]] = []
    for candidate, result in zip(known, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            log.warning("%s download failed: %s", candidate.source, type(result).__name__)
        else:
            downloaded.append((candidate, result))
    return downloaded


def _pick(ranked: Sequence[Candidate]) -> list[Candidate]:
    """The first `RETIME_PER_LANGUAGE` candidates of each language, in ranked order."""
    counts: dict[str, int] = {}
    picked = []
    for candidate in ranked:
        if counts.get(candidate.language, 0) < RETIME_PER_LANGUAGE:
            counts[candidate.language] = counts.get(candidate.language, 0) + 1
            picked.append(candidate)
    return picked


async def retime(
    stages: Stages,
    sources: Sequence[SubtitleSource],
    client: httpx.AsyncClient,
    config: UserConfig,
    video: VideoId,
    extra: Extra,
    ranked: Sequence[Candidate],
) -> list[Retimed]:
    """Re-time the top subtitle of each language. Only subtitles that need it are returned."""
    if not ranked or (extra.filename is None and extra.video_size is None):
        return []
    provider = stages.debrid(config, client)
    if provider is None:
        return []
    stream = await stages.find_stream(client, video, extra.filename, extra.video_size)
    if stream is None:
        return []
    url = await provider.direct_url(stream)
    if url is None:
        return []

    downloaded, timelines = await asyncio.gather(
        _download_all(sources, _pick(ranked)), _prepare_audio(stages, url)
    )
    if timelines is None:
        return []

    retimed = []
    for candidate, subtitle in downloaded:
        correction = await asyncio.to_thread(stages.align, timelines, subtitle.cues)
        if correction.verdict is not Verdict.SYNCED:
            continue
        retimed.append(
            Retimed(
                candidate,
                correction,
                write_srt(subtitle.cues),
                write_srt(apply_correction(subtitle.cues, correction)),
            )
        )
    return retimed
