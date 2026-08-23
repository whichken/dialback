"""Request context shared across the router."""
from __future__ import annotations

import asyncio
import dataclasses


@dataclasses.dataclass
class Request:
    """A normalized intercepted connection/request."""

    method: str
    path: str
    host: str | None          # from Host header, lowercased, port stripped
    raw_head: bytes           # exact bytes the client sent (up to blank line)
    orig_dst: tuple[str, int] | None  # pre-redirect destination (ip, port)
    era: str                  # resolved era label, e.g. "1997"
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    @property
    def client_ip(self) -> str | None:
        peer = self.writer.get_extra_info("peername")
        return peer[0] if peer else None
