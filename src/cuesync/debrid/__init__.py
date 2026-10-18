"""The DebridProvider protocol and the debrid clients (Real-Debrid, TorBox)."""

from __future__ import annotations

from cuesync.debrid.base import DebridProvider, debrid_provider
from cuesync.debrid.realdebrid import RealDebrid
from cuesync.debrid.torbox import TorBox

__all__ = ["DebridProvider", "RealDebrid", "TorBox", "debrid_provider"]
