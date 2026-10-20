"""The interface of a debrid provider, and the factory that builds the Real-Debrid one."""

from __future__ import annotations

from typing import Protocol

import httpx

from cuesync.config import UserConfig
from cuesync.debrid.realdebrid import RealDebrid
from cuesync.stream import StreamRef


class DebridProvider(Protocol):
    name: str

    async def direct_url(self, stream: StreamRef) -> str | None:
        """Direct HTTPS link to the file. Failures are logged and give None."""
        ...


def debrid_provider(config: UserConfig, client: httpx.AsyncClient) -> DebridProvider | None:
    """The Real-Debrid client for the user's key, or None when the user has no key."""
    if config.debrid_api_key:
        return RealDebrid(config.debrid_api_key, client)
    return None
