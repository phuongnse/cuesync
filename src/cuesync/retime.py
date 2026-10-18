"""Re-time the best subtitle of each language to the audio of the played file.

The stages run in order: stream lookup, debrid link (or the torrent stage, when the user has
no debrid service), audio windows and sync. Every stage is injectable so tests can fake it. A
subtitle that could not be aligned is missing from the result, so the caller falls back to the
original. The debrid link is a secret and is never logged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
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
from cuesync.stremio import Extra, VideoId
from cuesync.subtitles import Cue
from cuesync.sync import Correction, align, audio_speech
from cuesync.torrent import Limits, TorrentError

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
    probe: Callable[..., Awaitable[MediaInfo]] = audio.probe
    extract_windows: Callable[..., Awaitable[list[AudioWindow]]] = audio.extract_windows
    align: Callable[[Sequence[tuple[float, np.ndarray]], Sequence[Cue]], Correction] = align
    # Opens the played file of a torrent as a local URL. None unless the torrent fallback is on.
    # It is used only for a user without a debrid service.
    torrent: Callable[[StreamRef], AbstractAsyncContextManager[str]] | None = None
    torrent_timeout_s: float = Limits().time_s


async def _prepare_audio(
    stages: Stages, source: AbstractAsyncContextManager[str], over_torrent: bool
) -> list[tuple[float, np.ndarray]] | None:
    """Read the audio windows once and turn each into a speech timeline.

    Over a torrent, the windows are read one at a time and each read may take long.
    """
    timeout = stages.torrent_timeout_s
    probe_args = {"timeout": timeout} if over_torrent else {}
    extract_args = {"max_concurrent": 1, "timeout_s": timeout} if over_torrent else {}
    try:
        async with source as url:
            info = await stages.probe(url, **probe_args)
            track = select_audio_track(info.audio_tracks, None)
            windows = await stages.extract_windows(
                url, info.duration_s, track=track, **extract_args
            )
    except (AudioError, TorrentError) as exc:
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


def pick(ranked: Sequence[Candidate]) -> list[Candidate]:
    """The first `RETIME_PER_LANGUAGE` candidates of each language, in ranked order."""
    counts: dict[str, int] = {}
    picked = []
    for candidate in ranked:
        if counts.get(candidate.language, 0) < RETIME_PER_LANGUAGE:
            counts[candidate.language] = counts.get(candidate.language, 0) + 1
            picked.append(candidate)
    return picked


async def _audio_source(
    stages: Stages,
    client: httpx.AsyncClient,
    config: UserConfig,
    video: VideoId,
    extra: Extra,
) -> tuple[AbstractAsyncContextManager[str], bool] | None:
    """Where to read the audio, and whether that is a torrent. None when there is no way.

    Debrid always wins. A user with a debrid service never falls back to the torrent, even when
    the debrid service gives no link.
    """
    provider = stages.debrid(config, client)
    if provider is None and stages.torrent is None:
        return None
    stream = await stages.find_stream(client, video, extra.filename, extra.video_size)
    if stream is None:
        return None
    if provider is None:
        assert stages.torrent is not None
        return stages.torrent(stream), True
    url = await provider.direct_url(stream)
    return None if url is None else (nullcontext(url), False)


async def _no_audio() -> None:
    return None


async def retime(
    stages: Stages,
    sources: Sequence[SubtitleSource],
    client: httpx.AsyncClient,
    config: UserConfig,
    video: VideoId,
    extra: Extra,
    ranked: Sequence[Candidate],
    *,
    cache: Cache,
) -> dict[Candidate, Correction]:
    """Re-time the top subtitle of each language and return the correction found for each.

    A correction is returned whatever its verdict. The caller decides which to use. A subtitle
    that could not be downloaded or aligned has no entry.

    Results already in the cache are reused. Only the subtitles without one are aligned, and
    the stream lookup and audio read are skipped when the cache has the file's speech timeline.
    """
    if not ranked or (extra.filename is None and extra.video_size is None):
        return {}
    picked = pick(ranked)
    video_hash = extra.video_hash
    known: dict[Candidate, Correction] = {}
    if video_hash:
        for candidate in picked:
            stored = cache.get_sync(video_hash, encode_key(candidate))
            if stored is not None:
                known[candidate] = stored
    todo = [candidate for candidate in picked if candidate not in known]

    timelines = None
    source = None
    cacheable = bool(video_hash) and extra.video_size is not None
    if todo:
        if cacheable:
            timelines = cache.get_timeline(video_hash, extra.video_size)
        if timelines is None:
            source = await _audio_source(stages, client, config, video, extra)
            if source is None:
                todo = []

    if todo:
        audio_task = (
            _prepare_audio(stages, *source)
            if timelines is None and source is not None
            else _no_audio()
        )
        downloaded, fresh = await asyncio.gather(_download_all(cache, sources, todo), audio_task)
        if fresh is not None:
            timelines = fresh
            if cacheable:
                cache.put_timeline(video_hash, extra.video_size, fresh)
        if timelines is not None:
            for candidate, cues in downloaded:
                correction = await asyncio.to_thread(stages.align, timelines, cues)
                known[candidate] = correction
                if video_hash:
                    cache.put_sync(video_hash, encode_key(candidate), correction)

    return known
