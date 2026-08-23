"""Dialback router server: accepts redirected connections and dispatches."""
from __future__ import annotations

import asyncio
import logging
import socket
import struct

from .request import Request
from .rules import RuleEngine

log = logging.getLogger("dialback.router")

# getsockopt(SOL_IP, SO_ORIGINAL_DST) returns the pre-REDIRECT destination
SO_ORIGINAL_DST = 80


def _original_dst(writer: asyncio.StreamWriter) -> tuple[str, int] | None:
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    try:
        raw = sock.getsockopt(socket.SOL_IP, SO_ORIGINAL_DST, 16)
        if len(raw) < 8:
            return None
        _family, port_net, addr = struct.unpack("HH4s8x", raw[:16])
        return socket.inet_ntoa(addr), socket.ntohs(port_net)
    except OSError:
        return None


def _parse_head(raw_head: bytes) -> tuple[str, str, str | None]:
    """Returns (method, path, host) from the request head bytes."""
    lines = raw_head.split(b"\r\n")
    request_line = lines[0].decode("latin-1", errors="replace")
    parts = request_line.split(" ")
    method = parts[0] if len(parts) > 0 else ""
    path = parts[1] if len(parts) > 1 else "/"

    host = None
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"host":
            host = value.strip().decode("latin-1", errors="replace").lower()
            if ":" in host:
                host = host.rsplit(":", 1)[0]
            break
    return method, path, host


class RouterServer:
    def __init__(self, engine: RuleEngine):
        self.engine = engine

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else "?"
        orig_dst = _original_dst(writer)
        try:
            raw_head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=30
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            writer.close()
            return

        method, path, host = _parse_head(raw_head)
        req = Request(
            method=method,
            path=path,
            host=host,
            raw_head=raw_head,
            orig_dst=orig_dst,
            era=self.engine.era,
            reader=reader,
            writer=writer,
        )

        provider_name, provider = self.engine.select(host)
        log.info("%s -> %s %s http://%s%s [era=%s]",
                 peer_ip, provider_name, method, host or "?", path, req.era)

        try:
            await provider.handle(req)
        except (ConnectionError, BrokenPipeError):
            pass  # client hung up mid-response; nothing to do
        except Exception:
            log.exception("provider '%s' crashed handling %s", provider_name, req)
            try:
                writer.close()
            except Exception:
                pass

    async def serve(self, bind_host: str, bind_port: int) -> None:
        server = await asyncio.start_server(self.handle, bind_host, bind_port)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
        log.info("dialback router listening on %s (era=%s, default=%s)",
                 addrs, self.engine.era, self.engine.default_provider)
        async with server:
            await server.serve_forever()
