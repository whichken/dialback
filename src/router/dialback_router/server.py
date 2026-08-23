"""Dialback router server: accepts redirected connections and dispatches."""
from __future__ import annotations

import asyncio
import collections
import datetime
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
    def __init__(self, engine: RuleEngine,
                 request_log: collections.deque | None = None,
                 admin=None):
        self.engine = engine
        self.request_log = request_log or collections.deque(maxlen=200)
        self.admin = admin  # AdminApp instance or None

    # ------------------------------------------------------------- intake --

    async def handle_intercepted(self, reader, writer):
        await self._handle(reader, writer, via_admin_listener=False)

    async def handle_admin_listener(self, reader, writer):
        await self._handle(reader, writer, via_admin_listener=True)

    async def _handle(self, reader, writer, via_admin_listener: bool):
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

        # --- control plane routing ---
        is_admin = (self.admin is not None and via_admin_listener) or (
            self.admin is not None and host == self.admin.hostname
        )
        if is_admin:
            req = self._make_request(method, path, host, raw_head,
                                     orig_dst, reader, writer)
            started = datetime.datetime.now()
            try:
                await self.admin.handle(req)
            except (ConnectionError, BrokenPipeError):
                pass
            except Exception:
                log.exception("admin handler crashed")
                try:
                    writer.close()
                except Exception:
                    pass
            self._record(peer_ip, "admin", method, host or "", path)
            return

        req = self._make_request(method, path, host, raw_head,
                                 orig_dst, reader, writer)
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
        self._record(peer_ip, provider_name, method, host or "", path)

    def _make_request(self, method, path, host, raw_head, orig_dst,
                      reader, writer) -> Request:
        return Request(
            method=method,
            path=path,
            host=host,
            raw_head=raw_head,
            orig_dst=orig_dst,
            era=self.engine.era,
            profile=self.engine.profile,
            reader=reader,
            writer=writer,
        )

    def _record(self, client, provider, method, host, path) -> None:
        self.request_log.append({
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "client": client,
            "provider": provider,
            "method": method,
            "host": host,
            "path": path,
            "era": self.engine.era,
        })

    # -------------------------------------------------------------- serve --

    async def serve(self, bind_host: str, bind_port: int,
                    admin_port: int | None = None) -> None:
        server = await asyncio.start_server(
            self.handle_intercepted, bind_host, bind_port)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
        servers = [server]
        if admin_port:
            asrv = await asyncio.start_server(
                self.handle_admin_listener, bind_host, admin_port)
            aaddrs = ", ".join(str(s.getsockname()) for s in asrv.sockets or [])
            log.info("admin UI on %s (hostname %s)", aaddrs,
                     getattr(self.admin, "hostname", "?"))
            servers.append(asrv)
        log.info("dialback router listening on %s (era=%s, default=%s)",
                 addrs, self.engine.era, self.engine.default_provider)
        async with asyncio.TaskGroup() as tg:
            for s in servers:
                tg.create_task(s.serve_forever())
