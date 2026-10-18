"""Tests for the torrent fallback settings."""

from __future__ import annotations

import sys

import pytest

from cuesync.app import create_app
from cuesync.torrent import Limits, torrent_settings

BAD_VALUES = [
    ("CUESYNC_TORRENT_FALLBACK", "yes"),
    ("CUESYNC_TORRENT_FALLBACK", "true"),
    ("CUESYNC_TORRENT_FALLBACK", "2"),
    ("CUESYNC_TORRENT_TIMEOUT_S", "0"),
    ("CUESYNC_TORRENT_TIMEOUT_S", "-5"),
    ("CUESYNC_TORRENT_TIMEOUT_S", "soon"),
    ("CUESYNC_TORRENT_TIMEOUT_S", "nan"),
    ("CUESYNC_TORRENT_TIMEOUT_S", "inf"),
    ("CUESYNC_TORRENT_MAX_MB", "0"),
    ("CUESYNC_TORRENT_MAX_MB", "1.5"),
    ("CUESYNC_TORRENT_MAX_MB", "lots"),
    ("CUESYNC_TORRENT_DOWN_KBPS", "-1"),
    ("CUESYNC_TORRENT_DOWN_KBPS", "fast"),
    ("CUESYNC_TORRENT_UP_KBPS", "-1"),
    ("CUESYNC_TORRENT_UP_KBPS", "0.5"),
]


def test_defaults_are_the_limits_of_the_reader():
    settings = torrent_settings({})
    assert settings.enabled is False
    assert settings.limits == Limits()


def test_blank_values_are_the_defaults():
    env = {
        "CUESYNC_TORRENT_FALLBACK": " ",
        "CUESYNC_TORRENT_TIMEOUT_S": "",
        "CUESYNC_TORRENT_MAX_MB": "  ",
        "CUESYNC_TORRENT_DOWN_KBPS": "",
        "CUESYNC_TORRENT_UP_KBPS": "",
    }
    settings = torrent_settings(env)
    assert (settings.enabled, settings.limits) == (False, Limits())


def test_valid_values_map_to_limits():
    settings = torrent_settings(
        {
            "CUESYNC_TORRENT_FALLBACK": "1",
            "CUESYNC_TORRENT_TIMEOUT_S": "90.5",
            "CUESYNC_TORRENT_MAX_MB": "200",
            "CUESYNC_TORRENT_DOWN_KBPS": "500",
            "CUESYNC_TORRENT_UP_KBPS": "0",
        }
    )
    assert settings.enabled is True
    assert settings.limits == Limits(
        time_s=90.5, max_bytes=200 * 1024 * 1024, download_rate=500 * 1024, upload_rate=0
    )


def test_fallback_zero_is_off():
    assert torrent_settings({"CUESYNC_TORRENT_FALLBACK": "0"}).enabled is False


@pytest.mark.parametrize(("name", "value"), BAD_VALUES)
def test_bad_value_is_refused(name, value):
    with pytest.raises(ValueError, match=name):
        torrent_settings({name: value})


@pytest.mark.parametrize(("name", "value"), BAD_VALUES)
def test_bad_value_stops_the_app_from_starting(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        create_app()


def test_fallback_without_libtorrent_stops_the_app_from_starting(monkeypatch):
    monkeypatch.setitem(sys.modules, "libtorrent", None)
    monkeypatch.setenv("CUESYNC_TORRENT_FALLBACK", "1")
    with pytest.raises(ValueError, match=r"cuesync\[torrent\]"):
        create_app()


def test_settings_do_not_import_libtorrent(monkeypatch):
    monkeypatch.setitem(sys.modules, "libtorrent", None)
    assert torrent_settings({"CUESYNC_TORRENT_FALLBACK": "1"}).enabled is True
