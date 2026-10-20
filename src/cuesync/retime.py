"""Re-time the subtitle the user picked to the audio of the played file.

The stages run in order: stream lookup, Real-Debrid link, audio windows and sync. Every stage
is injectable so tests can fake it. A subtitle that could not be aligned gives no result, so the
caller falls back to the original. The Real-Debrid link is a secret and is never logged.
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
from cuesync.cache import Cache
from cuesync.config import UserConfig
from cuesync.debrid import DebridProvider, debrid_provider
from cuesync.search import encode_key
from cuesync.sources.base import Candidate, SubtitleSource
from cuesync.stream import StreamRef, find_stream
from cuesync.stremio import Extra, VideoId, video_key
from cuesync.subtitles import Cue
from cuesync.sync import Correction, align, audio_speech

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stages:
    """The work `retime` does. The defaults are the real functions."""

    find_stream: Callable[
        [httpx.AsyncClient, VideoId, str | None, int | None], Awaitable[StreamRef | None]
    ] = find_stream
    debrid: Callable[[UserConfig, httpx.AsyncClient], DebridProvider | None] = debrid_provider
    probe: Callable[..., Awaitable[MediaInfo]] = audio.probe
    extract_windows: Callable[..., Awaitable[list[AudioWindow]]] = audio.extract_windows
    align: Callable[[Sequence[tuple[float, np.ndarray]], Sequence[Cue]], Correction] = align


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
    cache: Cache, sources: Sequence[SubtitleSource], wanted: Sequence[Candidate]
) -> list[tuple[Candidate, tuple[Cue, ...]]]:
    """The cues of each wanted candidate, from the cache or else from its source."""
    by_name = {source.name: source for source in sources}
    stored = {candidate: cache.get_subtitle(encode_key(candidate)) for candidate in wanted}
    missing = [c for c in wanted if stored[c] is None and c.source in by_name]
    results = await asyncio.gather(
        *(by_name[candidate.source].download(candidate) for candidate in missing),
        return_exceptions=True,
    )
    for candidate, result in zip(missing, results, strict=True):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            log.warning("%s download failed: %s", candidate.source, type(result).__name__)
        else:
            cache.put_subtitle(encode_key(candidate), result.cues)
            stored[candidate] = result.cues
    return [(candidate, cues) for candidate, cues in stored.items() if cues is not None]


async def _audio_source(
    stages: Stages,
    client: httpx.AsyncClient,
    config: UserConfig,
    video: VideoId,
    extra: Extra,
) -> str | None:
    """The Real-Debrid link to the played file. None when there is no way to get one."""
    provider = stages.debrid(config, client)
    if provider is None:
        return None
    stream = await stages.find_stream(client, video, extra.filename, extra.video_size)
    if stream is None:
        return None
    return await provider.direct_url(stream)


async def _no_audio() -> None:
    return None


async def retime(
    stages: Stages,
    sources: Sequence[SubtitleSource],
    client: httpx.AsyncClient,
    config: UserConfig,
    video: VideoId,
    extra: Extra,
    candidate: Candidate,
    *,
    cache: Cache,
) -> Correction | None:
    """Re-time one subtitle and return the correction found for it.

    A correction is returned whatever its verdict. The caller decides whether to use it. None
    means the subtitle could not be downloaded or aligned.

    A result already in the cache is reused. The stream lookup and audio read are skipped when
    the cache has the file's speech timeline.
    """
    if extra.filename is None and extra.video_size is None:
        return None
    vkey = video_key(extra)
    key = encode_key(candidate)
    if vkey:
        stored = cache.get_sync(vkey, key)
        if stored is not None:
            return stored

    # A derived key already includes the size, so only a hash needs a size to be cacheable.
    cacheable = vkey is not None and (extra.video_size is not None or vkey != extra.video_hash)
    size = extra.video_size or 0
    timelines = cache.get_timeline(vkey, size) if cacheable else None
    url = None
    if timelines is None:
        url = await _audio_source(stages, client, config, video, extra)
        if url is None:
            return None

    audio_task = (
        _prepare_audio(stages, url) if timelines is None and url is not None else _no_audio()
    )
    downloaded, fresh = await asyncio.gather(_download_all(cache, sources, [candidate]), audio_task)
    if fresh is not None:
        timelines = fresh
        if cacheable:
            cache.put_timeline(vkey, size, fresh)
    if timelines is None or not downloaded:
        return None
    correction = await asyncio.to_thread(stages.align, timelines, downloaded[0][1])
    if vkey:
        cache.put_sync(vkey, key, correction)
    return correction
