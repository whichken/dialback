"""Archive provider: serves real Wayback Machine snapshots for the era.

For each request:
  1. CDX lookup for captures within the era's date range (cached)
  2. pick the capture closest to the era midpoint
  3. fetch original content (id_ flag = no Wayback toolbar) (cached)
  4. rewrite https:// links down to http:// so retro browsers can follow them
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import urllib.parse
import urllib.request

from .base import Provider, register
from ..cache import DiskCache
from ..request import Request

log = logging.getLogger("dialback.archive")

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
FETCH_TIMEOUT = 120   # web.archive.org can be VERY slow; be patient
FETCH_RETRIES = 1
CDX_TIMEOUT = 20


def _http_get(url: str, timeout: int) -> tuple[int, str, bytes]:
    """Blocking GET; returns (status, content_type, body).
    Raises OSError/TimeoutError on network failure."""
    req = urllib.request.Request(url, headers={"User-Agent": "dialback/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", b""


@register
class Archive(Provider):
    name = "archive"

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        cache_root = self.options.get("cache_root", "/var/cache/dialback")
        self.cdx_cache = DiskCache(cache_root)
        # separate namespaces via prefixing; one DiskCache instance is enough
        self.content_cache = DiskCache(cache_root)

    async def handle(self, req: Request) -> None:
        if not req.host or not req.profile:
            await self._serve_error(req, 400, "bad request")
            return

        original_url = f"http://{req.host}{req.path}"
        snapshot = await self._find_snapshot(req, original_url)
        if snapshot is None:
            await self._serve_not_archived(req)
            return
        timestamp, capture_url = snapshot

        ident = f"{timestamp}|{capture_url}"
        cached = self.content_cache.get("content", ident)
        if cached is None:
            fetch_url = (
                f"https://web.archive.org/web/{timestamp}id_/{capture_url}"
            )
            log.info("fetching %s", fetch_url)
            status = ctype = body = None
            for attempt in range(FETCH_RETRIES + 1):
                try:
                    status, ctype, body = await asyncio.to_thread(
                        _http_get, fetch_url, FETCH_TIMEOUT
                    )
                    break
                except (OSError, TimeoutError) as e:
                    log.warning("fetch attempt %d failed: %s", attempt + 1, e)
                    if attempt < FETCH_RETRIES:
                        await asyncio.sleep(2)
            if status is None or status != 200 or not body:
                await self._serve_unreachable(req)
                return
            if "text/html" in (ctype or ""):
                body = _downgrade_https(body)
            cached = (ctype or "").encode() + b"\n" + body
            self.content_cache.put("content", ident, cached)

        ctype, _, body = cached.partition(b"\n")
        await self._serve(req, 200, ctype.decode(errors="replace"), body,
                        head_only=(req.method == "HEAD"))

    # ---------------------------------------------------------------- CDX --

    async def _find_snapshot(self, req: Request, original_url: str):
        """Returns (timestamp, original_url_of_capture) or None."""
        prof = req.profile
        cdx_url = (
            f"{CDX_ENDPOINT}?url={urllib.parse.quote(original_url, safe='')}"
            f"&output=json&fl=timestamp,original,statuscode"
            f"&filter=statuscode:200&from={prof.start}&to={prof.end}&limit=-20"
        )
        cached = self.cdx_cache.get("cdx", cdx_url)
        if cached is None:
            log.info("cdx lookup %s [%s-%s]", original_url, prof.start, prof.end)
            try:
                status, _, body = await asyncio.to_thread(_http_get, cdx_url, CDX_TIMEOUT)
            except (OSError, TimeoutError) as e:
                log.warning("cdx lookup failed: %s", e)
                return None
            rows = json.loads(body or "[]") if status == 200 else []
            cached = json.dumps(rows).encode()
            self.cdx_cache.put("cdx", cdx_url, cached)
        else:
            rows = json.loads(cached)

        captures = [(int(r[0]), r[1]) for r in rows[1:] if len(r) >= 2]
        if not captures:
            return None

        target = (prof.start + prof.end) // 2
        ts, orig = min(captures, key=lambda c: abs(c[0] - target))
        # normalize truncated timestamps (CDX may return e.g. 1997 only)
        return f"{ts:014d}", orig

    # -------------------------------------------------------------- output --

    async def _serve(self, req: Request, status: int, ctype: str, body: bytes,
                     head_only: bool = False) -> None:
        reason = {200: "OK", 404: "Not Found", 400: "Bad Request",
                  502: "Bad Gateway"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        req.writer.write(head + (b"" if head_only else body))
        await req.writer.drain()

    async def _serve_page(self, req: Request, title: str, message: str,
                          status: int = 404) -> None:
        h = html.escape
        body = f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><meta charset="utf-8"><title>Dialback - {h(title)}</title></head>
<body bgcolor="#c0c0c0"><center>
<table width="600" cellpadding="10" border="2" bgcolor="#ffffff">
<tr><td bgcolor="#000080"><font color="#ffffff" size="5"><b>Dialback</b></font></td></tr>
<tr><td><h3>{h(title)}</h3>
<p>{message}</p>
<p><i>Era: {h(req.profile.label if req.profile else "?")}</i></p>
</td></tr></table></center></body></html>""".encode()
        await self._serve(req, status, "text/html", body)

    async def _serve_not_archived(self, req: Request) -> None:
        host = html.escape(req.host or "")
        msg = (f"<b>{host}</b> was not archived at this point in time "
               f"(or the archive is unreachable). Nothing here... yet.")
        await self._serve_page(req, "Not in the Archive", msg, 404)

    async def _serve_unreachable(self, req: Request) -> None:
        msg = "The archive could not be reached in time. Try again - it is<br>often slow on the first request, then served from local cache."
        await self._serve_page(req, "The Archive is Sleeping", msg, 502)

    async def _serve_error(self, req: Request, code: int, msg: str) -> None:
        await self._serve_page(req, "Error", html.escape(msg), code)


def _downgrade_https(body: bytes) -> bytes:
    """Rewrite https links to http so era browsers can follow them."""
    out = body.replace(b"https://", b"http://")
    # protocol-relative attribute URLs: src="//host/path" etc.
    return out.replace(b'="//', b'="http://')
