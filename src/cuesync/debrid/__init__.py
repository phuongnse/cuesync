"""The DebridProvider protocol and the debrid clients (Real-Debrid)."""

from __future__ import annotations

from cuesync.debrid.base import DebridProvider, debrid_provider
from cuesync.debrid.realdebrid import RealDebrid

__all__ = ["DebridProvider", "RealDebrid", "debrid_provider"]
