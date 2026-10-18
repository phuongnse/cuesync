"""The interface every debrid provider implements, and the factory that picks one."""

from __future__ import annotations

from typing import Protocol

import httpx

from cuesync.config import UserConfig
from cuesync.debrid.realdebrid import RealDebrid
from cuesync.debrid.torbox import TorBox
from cuesync.stream import StreamRef


class DebridProvider(Protocol):
    name: str

    async def direct_url(self, stream: StreamRef) -> str | None:
        """Direct HTTPS link to the file. Failures are logged and give None."""
        ...


def debrid_provider(config: UserConfig, client: httpx.AsyncClient) -> DebridProvider | None:
    """The provider the user configured, or None when there is none or it is not supported."""
    if config.debrid_provider == "realdebrid" and config.debrid_api_key:
        return RealDebrid(config.debrid_api_key, client)
    if config.debrid_provider == "torbox" and config.debrid_api_key:
        return TorBox(config.debrid_api_key, client)
    return None
