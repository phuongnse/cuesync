"""The DebridProvider protocol and the Real-Debrid client."""

from __future__ import annotations

from cuesync.debrid.base import DebridProvider, debrid_provider
from cuesync.debrid.realdebrid import RealDebrid

__all__ = ["DebridProvider", "RealDebrid", "debrid_provider"]
