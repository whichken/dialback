"""Background crawler: warm the cache with every page/asset of one domain.

Breadth-first walk starting at the domain root, staying within the domain
and its own subdomains (e.g. images.geocities.com counts; externals don't).
Every discovered page is resolved against the Wayback CDX for the current
dialed era and stored in the content cache; same-scope assets are fetched
too, so a later browse of the site is near-instant.
"""
from __future__ import annotations

import asyncio
import collections
import datetime
import logging
import re
import urllib.parse

from . import providers

log = logging.getLogger("dialback.crawler")

_LINK_RE = re.compile(r'<a\b[^>]*href\s*=\s*["\']?([^"\' >]+)', re.I)
_ASSET_RE = re.compile(
    r'(?:\bsrc|background)\s*=\s*["\']([^"\' >]+)'
    r'|<link[^>]+href=["\']([^"\']+\.css[^"\']*)', re.I)

_HOST_OK = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$")

# caps to keep a crawl bounded and polite
MAX_QUEUE = 200
PAGE_DELAY_S = 1.0
ASSETS_PER_PAGE = 12


def _norm(host: str) -> str:
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _in_scope(netloc: str, domain: str) -> bool:
    h, d = _norm(netloc), _norm(domain)
    return h == d or h.endswith("." + d)


def _absolutize(raw: str, base_url: str) -> str | None:
    raw = raw.strip().replace("https://", "http://")
    if not raw or raw.startswith(("data:", "#", "mailto:", "javascript:",
                                  "ftp:", "news:")):
        return None
    absu = urllib.parse.urljoin(base_url, raw)
    p = urllib.parse.urlparse(absu)
    if p.scheme != "http" or not p.netloc or not p.path:
        return None
    return absu


def _extract(html_text: str, base_url: str):
    """Returns (links, assets): [(host, path)], [absolute asset urls]."""
    links, assets = [], []
    seen_l, seen_a = set(), set()
    for m in _LINK_RE.finditer(html_text):
        absu = _absolutize(m.group(1), base_url)
        if not absu:
            continue
        p = urllib.parse.urlparse(absu)
        key = (p.netloc.lower(), p.path or "/")
        if key not in seen_l:
            seen_l.add(key)
            links.append(key)
    for m in _ASSET_RE.finditer(html_text):
        raw = m.group(1) or m.group(2) or ""
        absu = _absolutize(raw, base_url)
        if not absu:
            continue
        p = urllib.parse.urlparse(absu)
        key = f"http://{p.netloc}{p.path}"
        if key not in seen_a:
            seen_a.add(key)
            assets.append(key)
    return links, assets


class Crawler:
    """One crawl job at a time; progress exposed as a plain dict."""

    def __init__(self, engine):
        self.engine = engine
        self.job: dict | None = None
        self._task: asyncio.Task | None = None

    def status(self) -> dict | None:
        return self.job

    def start(self, host: str, max_pages: int = 30) -> tuple[bool, str]:
        host = host.strip().lower()
        if self.job and self.job.get("status") == "running":
            return False, "A crawl is already running."
        if not _HOST_OK.match(host):
            return False, f"'{host}' doesn't look like a domain name."
        try:
            max_pages = min(max(int(max_pages), 5), 150)
        except (TypeError, ValueError):
            max_pages = 30
        self.job = {
            "host": host,
            "max_pages": max_pages,
            "era": self.engine.era,
            "status": "running",
            "started": datetime.datetime.now().strftime("%H:%M:%S"),
            "pages": 0, "ok": 0, "miss": 0, "errors": 0, "assets": 0,
            "queued": 1, "current": f"http://{host}/",
        }
        self._task = asyncio.create_task(self._run(self.job))
        log.info("crawl started: %s (max %d pages, era %s)",
                 host, max_pages, self.engine.era)
        return True, f"Crawl of {host} started."

    def cancel(self) -> bool:
        if self._task and not self._task.done():
            self._task.cancel()
            return True
        return False

    def _archive(self):
        inst = self.engine._instances.get("archive")
        if inst is None:
            inst = providers.create("archive")
            self.engine._instances["archive"] = inst
        return inst

    async def _run(self, job: dict) -> None:
        domain = job["host"]
        prof = self.engine.profile
        archive = self._archive()
        queue: collections.deque = collections.deque([("", "/")])
        # ("", "/") means the bare domain; resolved below
        seen: set[tuple[str, str]] = set()

        def norm_key(host: str, path: str) -> tuple[str, str]:
            return (_norm(host or domain), path or "/")

        queue.clear()
        queue.append((domain, "/"))
        try:
            while queue and job["pages"] < job["max_pages"]:
                host, path = queue.popleft()
                key = (host, path)
                if key in seen:
                    continue
                seen.add(key)
                job["current"] = f"http://{host}{path}"

                snapshot = await archive._find_snapshot_for(
                    prof, f"http://{host}{path}")
                if snapshot is None:
                    job["miss"] += 1
                    job["pages"] += 1
                    continue
                timestamp, capture_url = snapshot
                ctype, body = await archive._content(timestamp, capture_url)
                job["pages"] += 1
                if body is None:
                    job["errors"] += 1
                    continue
                job["ok"] += 1

                if "text/html" in (ctype or "") and body:
                    try:
                        text = body.decode(errors="replace")
                        links, assets = _extract(text, capture_url)
                    except Exception:
                        links, assets = [], []
                    for asset_url in assets:
                        if job["assets"] >= ASSETS_PER_PAGE * job["max_pages"]:
                            break
                        p = urllib.parse.urlparse(asset_url)
                        if _in_scope(p.netloc, domain):
                            try:
                                await archive._prefetch_one(prof, asset_url)
                                job["assets"] += 1
                            except Exception:
                                pass
                    for lh, lp in links:
                        if not _in_scope(lh, domain):
                            continue
                        k = (_norm(lh), lp)
                        if k in seen or len(seen) + len(queue) >= MAX_QUEUE:
                            continue
                        queue.append((_norm(lh), lp))
                    job["queued"] = len(queue)

                await asyncio.sleep(PAGE_DELAY_S)

            job["status"] = "done" if job["ok"] else "nothing archived"
            log.info("crawl finished: %s (%d pages, %d assets)",
                     domain, job["ok"], job["assets"])
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except Exception as e:
            job["status"] = "error"
            job["error"] = str(e)
            log.exception("crawl of %s crashed", domain)
