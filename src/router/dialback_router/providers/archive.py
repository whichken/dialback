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
import http.client
import json
import logging
import queue
import re
import time
import urllib.parse

from .base import Provider, create, register
from ..cache import DiskCache
from ..request import Request

log = logging.getLogger("dialback.archive")

CDX_HOST = "web.archive.org"
FETCH_TIMEOUT = 120   # web.archive.org can be VERY slow; be patient
FETCH_RETRIES = 1
CDX_TIMEOUT = 20

# politeness: never more than this many in-flight wayback operations
WAYBACK_CONCURRENCY = 4
_wayback_sem: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _wayback_sem
    if _wayback_sem is None:
        _wayback_sem = asyncio.Semaphore(WAYBACK_CONCURRENCY)
    return _wayback_sem


# ---------------------------------------------------- connection pooling --

_CONN_POOL: queue.LifoQueue = queue.LifoQueue(maxsize=8)


def _get_conn() -> http.client.HTTPSConnection:
    try:
        return _CONN_POOL.get_nowait()
    except queue.Empty:
        return http.client.HTTPSConnection(CDX_HOST, timeout=FETCH_TIMEOUT)


def _release_conn(conn: http.client.HTTPSConnection) -> None:
    try:
        if conn.sock is not None:
            _CONN_POOL.put_nowait(conn)
            return
    except (queue.Full, OSError, AttributeError):
        pass
    _discard(conn)


def _discard(conn: http.client.HTTPSConnection) -> None:
    try:
        conn.close()
    except OSError:
        pass


def _http_get(url: str, timeout: int) -> tuple[int, str, bytes, dict]:
    """Blocking keep-alive GET. Returns (status, content_type, body, headers).
    Raises OSError/TimeoutError on network failure."""
    path = urllib.parse.urlparse(url).path
    if urllib.parse.urlparse(url).query:
        path += "?" + urllib.parse.urlparse(url).query
    for attempt in range(3):
        conn = _get_conn()
        try:
            conn.timeout = max(timeout, conn.timeout)
            conn.request("GET", path or "/",
                         headers={"User-Agent": "dialback/0.1",
                                  "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            body = resp.read()
            headers = {k.lower(): v for k, v in resp.getheaders()}
            status = resp.status
            ctype = headers.get("content-type", "")
            _release_conn(conn)
            if status == 429:
                # rate limited: honor Retry-After once, bounded
                delay = min(float(headers.get("retry-after", 5) or 5), 30)
                log.warning("rate limited by archive.org; backing off %.0fs", delay)
                import time
                time.sleep(delay)
                continue
            return status, ctype, body, headers
        except (OSError, TimeoutError, http.client.HTTPException) as e:
            _discard(conn)
            # pooled connections may have been closed server-side; retry once
            # on a fresh socket before giving up
            if attempt < 2:
                log.debug("connection error (%s); retrying on fresh socket", e)
                import time
                time.sleep(0.5 * attempt)
                continue
            raise


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

        ctype, body = await self._content(timestamp, capture_url)
        if body is None:
            await self._serve_unreachable(req)
            return

        if "text/html" in (ctype or ""):
            self._schedule_prefetch(req, capture_url, body)

        body = self.transform(req, ctype or "", body)
        await self._serve(req, 200, ctype or "", body,
                          head_only=(req.method == "HEAD"))

    # ---------------------------------------------------------- fetching --

    async def _content(self, timestamp: str, capture_url: str):
        """Returns (ctype, body) from cache or wayback; body None on failure."""
        ident = f"{timestamp}|{capture_url}"
        cached = self.cache.get("content", ident)
        if cached is not None:
            ctype, _, body = cached.partition(b"\n")
            return ctype.decode(errors="replace"), body

        fetch_url = f"https://{CDX_HOST}/web/{timestamp}id_/{capture_url}"
        log.info("fetching %s", fetch_url)
        status = ctype = body = None
        for attempt in range(FETCH_RETRIES + 1):
            try:
                t0 = time.monotonic()
                async with _sem():
                    status, ctype, body, _hdrs = await asyncio.to_thread(
                        _http_get, fetch_url, FETCH_TIMEOUT
                    )
                elapsed = time.monotonic() - t0
                if elapsed > 10:
                    log.warning("slow content fetch (%.1fs): %s",
                                elapsed, capture_url)
                break
            except (OSError, TimeoutError) as e:
                log.warning("fetch attempt %d failed: %s", attempt + 1, e)
                if attempt < FETCH_RETRIES:
                    await asyncio.sleep(2)
        if status != 200 or not body:
            return (ctype or ""), None
        if "text/html" in (ctype or ""):
            body = _downgrade_https(body)
        self.cache.put("content", ident,
                       (ctype or "").encode() + b"\n" + body)
        return (ctype or ""), body

    # ------------------------------------------------------------ prefetch --

    def _schedule_prefetch(self, req: Request, base_url: str,
                           html_body: bytes) -> None:
        """Resolve+cache same-host assets before the browser asks for them.

        Deliberately polite: waits for the interactive response to go out,
        then staggers work so prefetch never starves live browsing.
        """
        try:
            assets = _extract_assets(html_body.decode(errors="replace"),
                                     base_url)
        except Exception:
            return
        for i, asset_url in enumerate(assets[:6]):
            task = asyncio.create_task(
                self._prefetch_one(req.profile, asset_url,
                                   delay=3.0 + i * 1.5))
            task.add_done_callback(lambda t: t.exception() and
                                   log.debug("prefetch error: %s", t.exception()))

    async def _prefetch_one(self, prof, asset_url: str, delay: float = 0.0):
        if delay:
            await asyncio.sleep(delay)
        snapshot = await self._find_snapshot_for(prof, asset_url)
        if snapshot is None:
            return
        await self._content(snapshot[0], snapshot[1])

    def transform(self, req: Request, ctype: str, body: bytes) -> bytes:
        """Hook for subclasses (hybrid) to post-process served content."""
        return body

    # ---------------------------------------------------------------- CDX --

    async def _find_snapshot(self, req: Request, original_url: str):
        """Returns (timestamp, original_url_of_capture) or None."""
        return await self._find_snapshot_for(req.profile, original_url)

    async def _find_snapshot_for(self, prof, original_url: str):
        cdx_url = (
            f"https://{CDX_HOST}/cdx/search/cdx"
            f"?url={urllib.parse.quote(original_url, safe='')}"
            f"&output=json&fl=timestamp,original,statuscode"
            f"&filter=statuscode:200&from={prof.start}&to={prof.end}&limit=-20"
        )
        cached = self.cache.get("cdx", cdx_url)
        if cached is None:
            log.info("cdx lookup %s [%s-%s]", original_url, prof.start, prof.end)
            t0 = time.monotonic()
            try:
                async with _sem():
                    status, _, body, _hdrs = await asyncio.to_thread(
                        _http_get, cdx_url, CDX_TIMEOUT)
            except (OSError, TimeoutError) as e:
                log.warning("cdx lookup failed after %.1fs: %s",
                            time.monotonic() - t0, e)
                return None
            elapsed = time.monotonic() - t0
            if elapsed > 5:
                log.warning("slow cdx lookup (%.1fs): %s", elapsed, original_url)
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


def _extract_assets(text: str, base_url: str) -> list[str]:
    """Same-host asset URLs referenced by an HTML page (img/css/background)."""
    from urllib.parse import urljoin, urlparse
    pattern = re.compile(
        r'(?:\bsrc|background)\s*=\s*["\']([^"\' >]+)'
        r'|<link[^>]+href=["\']([^"\']+\.css[^"\']*)', re.I)
    base_host = urlparse(base_url).netloc.lower()
    seen, out = set(), []
    for m in pattern.finditer(text):
        raw = (m.group(1) or m.group(2) or "").strip()
        if not raw or raw.startswith(("data:", "#", "mailto:", "javascript:")):
            continue
        absu = urljoin(base_url, raw.replace("https://", "http://"))
        p = urlparse(absu)
        if p.netloc.lower() != base_host or not p.path:
            continue
        if any(p.path.lower().endswith(ext) for ext in
               (".gif", ".jpg", ".jpeg", ".png", ".css", ".ico")):
            key = f"http://{p.netloc}{p.path}"
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out

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
