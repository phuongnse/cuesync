"""Concurrent source search, ranking and subtitle keys."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence

import httpx

from cuesync.cache import Cache
from cuesync.config import UserConfig
from cuesync.sources.base import Candidate, SubtitleRequest, SubtitleSource
from cuesync.sources.subdl import SubDLSource
from cuesync.sync import Correction, Verdict

# Stremio gives up on slow subtitle responses, so a slow source is left out.
SEARCH_BUDGET = 5.0
# The most subtitles one request lists.
MAX_SUBTITLES = 5
EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".srt", ".ass", ".ssa", ".vtt", ".sub"})
TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")

log = logging.getLogger(__name__)


def build_sources(config: UserConfig, client: httpx.AsyncClient) -> list[SubtitleSource]:
    """The sources the user has configured: SubDL when there is a SubDL key."""
    source = SubDLSource.from_config(config, client)
    return [source] if source is not None else []


async def gather_candidates(
    sources: Sequence[SubtitleSource],
    request: SubtitleRequest,
    budget: float = SEARCH_BUDGET,
    cache: Cache | None = None,
) -> list[Candidate]:
    """Search all sources at once. Sources that fail or miss the budget add nothing.

    A source whose answer is in the cache is not asked again.
    """
    if not sources:
        return []
    cached = {
        index: hit
        for index, source in enumerate(sources)
        if cache is not None and (hit := cache.get_search(source.name, request)) is not None
    }
    tasks = {
        index: asyncio.ensure_future(source.search(request))
        for index, source in enumerate(sources)
        if index not in cached
    }
    pending: set[asyncio.Future] = set()
    if tasks:
        _, pending = await asyncio.wait(tasks.values(), timeout=budget)
    for task in pending:
        task.cancel()
    candidates: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for index, source in enumerate(sources):
        found = cached.get(index)
        if found is None:
            task = tasks[index]
            if task in pending:
                log.warning("%s did not answer within %.1f s, leaving it out", source.name, budget)
                continue
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                log.warning("%s search failed: %s", source.name, type(error).__name__)
                continue
            found = task.result()
            if cache is not None:
                cache.put_search(source.name, request, found)
        for candidate in found:
            key = (candidate.source, candidate.source_id)
            if key not in seen:
                seen.add(key)
                candidates.append(candidate)
    return candidates


def _tokens(name: str) -> set[str]:
    name = name.lower()
    stem, extension = os.path.splitext(name)
    if extension in EXTENSIONS:
        name = stem
    return {token for token in TOKEN_SPLIT.split(name) if token}


def similarity(filename: str | None, release_name: str) -> float:
    """How alike two release names are, from 0 to 1 (Jaccard overlap of their tokens)."""
    if filename is None:
        return 0.0
    a = _tokens(filename)
    b = _tokens(release_name)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def rank(
    candidates: list[Candidate], languages: Sequence[str], filename: str | None, limit: int
) -> list[Candidate]:
    """Group by language in the user's order. Within a language, hash matches come first,
    then the closest release name, then the most downloaded."""
    ranked: list[Candidate] = []
    for language in languages:
        group = [c for c in candidates if c.language == language]
        group.sort(
            key=lambda c: (not c.hash_match, -similarity(filename, c.release_name), -c.downloads)
        )
        ranked.extend(group[:limit])
    return ranked


def order_by_fit(
    candidates: Sequence[Candidate], results: Mapping[str, Correction]
) -> list[Candidate]:
    """Put the subtitles measured against the audio first, the best fit first.

    Then come the ones not measured yet, in their given order, and the failed ones last.
    """

    def order(candidate: Candidate) -> tuple[int, float]:
        result = results.get(encode_key(candidate))
        if result is None:
            return (1, 0.0)
        if result.verdict is Verdict.FAILED:
            return (2, 0.0)
        return (0, -result.confidence)

    return sorted(candidates, key=order)


def encode_key(candidate: Candidate) -> str:
    """An opaque key for a candidate. It holds no secrets, so links work after a restart."""
    raw = json.dumps(
        [candidate.source, candidate.source_id, candidate.language], separators=(",", ":")
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_key(key: str) -> tuple[str, str, str]:
    """Return (source, source id, language). Raise ValueError if the key is malformed."""
    try:
        padded = key + "=" * (-len(key) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError("malformed subtitle key") from None
    if (
        not isinstance(data, list)
        or len(data) != 3
        or not all(isinstance(item, str) for item in data)
    ):
        raise ValueError("malformed subtitle key")
    source, source_id, language = data
    return source, source_id, language
