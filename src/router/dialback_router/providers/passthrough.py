"""Passthrough provider: transparent TCP relay to the original destination.

This is stage-2 scaffolding that keeps the retro PC on the REAL internet
through the router. Later stages replace it (by rule or by default) with
archive/LLM providers.
"""
from __future__ import annotations

import asyncio

from .base import Provider, register
from ..request import Request

CHUNK = 65536
CONNECT_TIMEOUT = 10  # seconds to establish the upstream connection


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(CHUNK)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, TimeoutError):
        pass


@register
class Passthrough(Provider):
    name = "passthrough"

    async def handle(self, req: Request) -> None:
        if not req.orig_dst:
            self._error(req, "no original destination recorded")
            return
        ip, port = req.orig_dst
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError) as e:
            self._error(req, f"upstream {ip}:{port} unreachable ({e})")
            return

        up_writer.write(req.raw_head)
        await up_writer.drain()

        c2u = asyncio.create_task(_pump(req.reader, up_writer))
        u2c = asyncio.create_task(_pump(up_reader, req.writer))
        done, pending = await asyncio.wait({c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for w in (up_writer, req.writer):
            try:
                w.close()
                await w.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _error(self, req: Request, msg: str) -> None:
        body = (
            "<html><body><h1>Dialback</h1>"
            f"<p>Could not reach upstream: {msg}</p></body></html>"
        ).encode()
        head = (
            "HTTP/1.1 502 Bad Gateway\r\n"
            "Content-Type: text/html\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode()
        req.writer.write(head + body)
