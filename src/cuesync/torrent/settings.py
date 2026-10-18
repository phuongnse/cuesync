"""The torrent fallback settings, read from the environment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cuesync.torrent.reader import Limits

KIB = 1024
MIB = 1024 * 1024


@dataclass(frozen=True)
class TorrentSettings:
    enabled: bool
    limits: Limits


def _value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return value.strip() if value and value.strip() else None


def _whole_number(env: Mapping[str, str], name: str, default: int, minimum: int) -> int:
    value = _value(env, name)
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        number = minimum - 1
    if number < minimum:
        kind = "a positive whole number" if minimum > 0 else "a whole number of 0 or more"
        raise ValueError(f"{name} must be {kind}, got {env[name]!r}")
    return number


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    value = _value(env, name)
    if value is None:
        return default
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    # `not seconds > 0` also rejects nan. inf is rejected so the limit is a real one.
    if not seconds > 0 or seconds == float("inf"):
        raise ValueError(f"{name} must be a positive number of seconds, got {env[name]!r}")
    return seconds


def torrent_settings(env: Mapping[str, str]) -> TorrentSettings:
    """The fallback switch and the read limits. Raises `ValueError` naming a bad variable."""
    switch = _value(env, "CUESYNC_TORRENT_FALLBACK")
    if switch not in (None, "0", "1"):
        raise ValueError(
            f"CUESYNC_TORRENT_FALLBACK must be 0 or 1, got {env['CUESYNC_TORRENT_FALLBACK']!r}"
        )
    defaults = Limits()
    limits = Limits(
        time_s=_seconds(env, "CUESYNC_TORRENT_TIMEOUT_S", defaults.time_s),
        max_bytes=_whole_number(env, "CUESYNC_TORRENT_MAX_MB", defaults.max_bytes // MIB, minimum=1)
        * MIB,
        download_rate=_whole_number(
            env, "CUESYNC_TORRENT_DOWN_KBPS", defaults.download_rate // KIB, minimum=0
        )
        * KIB,
        upload_rate=_whole_number(
            env, "CUESYNC_TORRENT_UP_KBPS", defaults.upload_rate // KIB, minimum=0
        )
        * KIB,
    )
    return TorrentSettings(switch == "1", limits)
