"""Search engine provider: era search engines that actually return results.

Personas (AltaVista, HotBot, Lycos, Yahoo) render authentic-looking result
pages for the user's real query. Results are synthesized by the LLM but
constrained to SITES - real domains Dialback can actually serve - with an
offline keyword-scoring fallback when no API key is configured.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import urllib.parse

from .base import Provider, register
from .llm import read_key, _post_chat
from ..cache import DiskCache
from ..request import Request
from .. import sites

log = logging.getLogger("dialback.search")

REQUEST_TIMEOUT = 60

# host substring -> (brand name, accent color, tagline, query param)
PERSONAS = {
    "altavista": ("AltaVista", "#1a3c8e", "Search the web with confidence", "q"),
    "hotbot": ("HotBot", "#cc0000", "Sweep the web", "query"),
    "lycos": ("Lycos", "#003366", "Catalog of the Internet", "query"),
    "yahoo": ("Yahoo!", "#7b0099", "The Web's Best Directory", "p"),
}


def _persona_for(host: str) -> tuple[str, str, str, str]:
    for key, persona in PERSONAS.items():
        if key in host:
            return persona
    return ("Search", "#333388", "Find it on the web", "q")


def _extract_query(path: str) -> str:
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    for param in ("q", "p", "query", "search", "s", "text"):
        if param in qs and qs[param][0].strip():
            return qs[param][0].strip()[:200]
    return ""


def _keyword_results(query: str, limit: int = 10) -> list[dict]:
    """Offline fallback: score the curated pool against query keywords."""
    words = set(re.findall(r"[a-z0-9]+", query.lower()))
    scored = []
    for domain, title, desc, keywords in sites.SITES:
        hay = f"{domain} {title} {desc} {keywords}".lower()
        score = sum(2 if w in hay else 0 for w in words)
        # small bias toward big-name sites so generic queries look sane
        score += 1 if domain.startswith("www.") else 0
        if score > 0:
            scored.append((score, domain, title, desc))
    scored.sort(reverse=True)
    results = []
    for score, domain, title, desc in scored[:limit]:
        results.append({
            "title": f"{title}",
            "url": f"http://{domain}/",
            "snippet": desc,
        })
    # pad with popular sites so the page never looks empty
    if len(results) < 5:
        for domain, title, desc, _kw in sites.SITES[:10]:
            url = f"http://{domain}/"
            if url not in [r["url"] for r in results]:
                results.append({"title": title, "url": url, "snippet": desc})
            if len(results) >= 5:
                break
    return results


def _llm_results(api_key: str, model: str, persona_brand: str,
                 query: str, target: str, max_tokens: int = 1500):
    """Blocking LLM call. Returns list of {title,url,snippet}."""
    pool = "\n".join(f"- {d}" for d, *_ in sites.SITES)
    system = (
        f"You simulate the {persona_brand} search engine as it existed in "
        f"the internet of {target}. Given a user query you produce a "
        "realistic list of results.\n"
        "STRICT RULES:\n"
        "- Use ONLY domains from this list (any page path on them):\n"
        f"{pool}\n"
        "- Titles and snippets must read like authentic {target} web pages\n"
        "- Respond with ONLY a JSON array, no markdown fences, objects with "
        'keys "title", "url", "snippet"; 6-10 entries'
    )
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Query: {query}"},
        ],
    }
    data = _post_chat(api_key, "https://openrouter.ai/api/v1", payload,
                      REQUEST_TIMEOUT)
    content = data["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?\s*\n?", "", content)
    content = re.sub(r"\n?```\s*$", "", content)
    arr = json.loads(content)
    valid_domains = {d for d, *_ in sites.SITES}
    out = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", ""))
        m = re.match(r"https?://([^/]+)", url)
        if m and (m.group(1) in valid_domains or
                  any(m.group(1).endswith("." + d) for d in valid_domains)):
            out.append({"title": str(item.get("title", url))[:120],
                        "url": url.replace("https://", "http://"),
                        "snippet": str(item.get("snippet", ""))[:300]})
    return out


@register
class Search(Provider):
    name = "search"

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        self.model = self.options.get("model", "gpt-5.6-luna")
        self.cache = DiskCache(self.options.get("cache_root",
                                               "/var/cache/dialback"))

    async def handle(self, req: Request) -> None:
        if req.method not in ("GET", "HEAD") or not req.host or not req.profile:
            await self._serve_page(req, "Bad request")
            return

        brand, accent, tagline, qparam = _persona_for(req.host.lower())
        query = _extract_query(req.path)

        ident = f"{req.profile.label}|{req.host}|{query}"
        cached = self.cache.get("search", ident)
        if cached is not None:
            body = cached
        elif not query:
            body = self._render_home(brand, accent, tagline, qparam)
            self.cache.put("search", ident, body)
        else:
            body = await self._results_page(req, brand, accent, tagline,
                                            qparam, query)
            self.cache.put("search", ident, body)

        head = ("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        req.writer.write(head + (b"" if req.method == "HEAD" else body))
        await req.writer.drain()

    async def _results_page(self, req: Request, brand: str, accent: str,
                            tagline: str, qparam: str, query: str) -> bytes:
        results = None
        api_key = read_key()
        if api_key:
            try:
                results = await asyncio.wait_for(
                    asyncio.to_thread(
                        _llm_results, api_key, self.model, brand,
                        query, req.profile.label),
                    timeout=75)
                log.info("llm results for %r (%d hits)", query, len(results))
            except Exception as e:
                log.warning("llm results failed (%s); keyword fallback", e)
        if results is None:
            results = _keyword_results(query)

        esc = html.escape
        rows = []
        for i, r in enumerate(results, 1):
            rows.append(
                f"<p><b>{i}.</b> <a href=\"{esc(r['url'])}\"><font size=\"4\" "
                f"color=\"#0000ee\"><b>{esc(r['title'])}</b></font></a><br>"
                f"<font size=\"2\">{esc(r['snippet'])}</font><br>"
                f"<font size=\"2\" color=\"#008000\">{esc(r['url'])}</font></p>")
        count = len(results)
        return (f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><meta charset="utf-8"><title>{esc(brand)}: {esc(query)}</title></head>
<body bgcolor="#ffffff" text="#000000" link="#0000ee" vlink="#551a8b">
<table width="100%" cellpadding="8"><tr><td bgcolor="{esc(accent)}">
<font color="#ffffff" size="6"><b>{esc(brand)}</b></font>
&nbsp;&nbsp;<font color="#ffffff">{esc(tagline)}</font></td></tr></table>
<form action="/" method="GET">
<p><input type="text" name="{esc(qparam)}" value="{esc(query)}" size="40">
<button type="submit">Search</button></p></form>
<hr>
<font size="2" color="#555555">About {count*37+13:,} documents match your
query. Showing top {count}.</font>
{''.join(rows)}
<hr>
<table width="100%" cellpadding="6"><tr><td bgcolor="#eeeeee">
<font size="2">Results generated {esc(req.profile.label)} |
<a href="/">New search</a> | {esc(brand)} &mdash; period-authentic search
powered by Dialback</font></td></tr></table>
</body></html>""").encode()

    def _render_home(self, brand: str, accent: str, tagline: str,
                     qparam: str) -> bytes:
        esc = html.escape
        return (f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><meta charset="utf-8"><title>{esc(brand)}</title></head>
<body bgcolor="#ffffff" text="#000000">
<center>
<table cellpadding="12" border="0"><tr><td align="center">
<font size="7" color="{esc(accent)}"><b>{esc(brand)}</b></font><br>
<font size="4">{esc(tagline)}</font>
<p>
<form action="/" method="GET">
<input type="text" name="{esc(qparam)}" size="45">
<button type="submit">Search</button>
</form>
</td></tr></table>
<table width="600" cellpadding="8" border="1">
<tr><td bgcolor="#ffffcc"><font size="2"><b>Tips:</b> use + to require a word,
- to exclude one, and quotes for phrases. The web is young &mdash; go find
something!</font></td></tr></table>
</center></body></html>""").encode()

    async def _serve_page(self, req: Request, msg: str) -> None:
        body = (f"<html><body><h1>Dialback</h1><p>{html.escape(msg)}</p>"
                "</body></html>").encode()
        head = ("HTTP/1.1 400 Bad Request\r\nContent-Type: text/html\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        req.writer.write(head + body)
