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

from .base import Provider, create, register
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
        max_mb = self.options.get("cache_max_mb")
        self.cache = DiskCache(
            cache_root,
            max_pct=float(self.options.get("cache_max_pct", 85.0)),
            max_bytes=int(max_mb) * 1024 * 1024 if max_mb else None,
        )
        # optional synthesis fallback when nothing is archived (stage 5)
        self._fallback = None
        fb_name = self.options.get("fallback_provider")
        if fb_name and fb_name != self.name:
            self._fallback = create(fb_name)

    async def handle(self, req: Request) -> None:
        if not req.host or not req.profile:
            await self._serve_error(req, 400, "bad request")
            return

        original_url = f"http://{req.host}{req.path}"
        snapshot = await self._find_snapshot(req, original_url)
        if snapshot is None:
            if self._fallback is not None:
                await self._fallback.handle(req)
            else:
                await self._serve_not_archived(req)
            return
        timestamp, capture_url = snapshot

        ident = f"{timestamp}|{capture_url}"
        cached = self.cache.get("content", ident)
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
            self.cache.put("content", ident, cached)

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
        cached = self.cache.get("cdx", cdx_url)
        if cached is None:
            log.info("cdx lookup %s [%s-%s]", original_url, prof.start, prof.end)
            try:
                status, _, body = await asyncio.to_thread(_http_get, cdx_url, CDX_TIMEOUT)
            except (OSError, TimeoutError) as e:
                log.warning("cdx lookup failed: %s", e)
                return None
            rows = json.loads(body or "[]") if status == 200 else []
            cached = json.dumps(rows).encode()
            self.cache.put("cdx", cdx_url, cached)
        else:
            rows = json.loads(cached)

        captures = [(int(r[0]), r[1]) for r in rows[1:] if len(r) >= 2]
        if not captures:
            return None

        target = prof.target
        ts, orig = min(captures, key=lambda c: abs(c[0] - target))
        # normalize truncated timestamps (CDX may return e.g. 1997 only)
        return f"{ts:014d}", orig

    # -------------------------------------------------------------- output --

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
