"""Hybrid provider: real archived pages, dressed with era chrome.

Subclasses Archive. When a snapshot exists, deterministic per-site filler
(visitor counter, guestbook link, badges) is injected so thin archived
pages feel like the living web of the era. On archive miss it falls back
exactly like its parent (LLM synthesis when configured).
"""
from __future__ import annotations

import hashlib
import re

from .base import Provider, register
from .archive import Archive


def _seed(host: str, path: str) -> int:
    return int(hashlib.sha1(f"{host}{path}".encode()).hexdigest()[:8], 16)


class _Chrome:
    """Era chrome fragments, stable per (host, path)."""

    def __init__(self, host: str, path: str, era: str):
        rnd = _seed(host, path)
        self.counter = 1000 + rnd % 989000          # visitor number
        self.last_updated = era                      # simple, honest-ish
        self.guestbook_id = rnd % 999

    def footer_html(self) -> str:
        return (
            '<hr><center><table cellpadding="6" border="1" bgcolor="#ffffcc">'
            "<tr><td align=\"center\"><font size=\"2\">"
            f"You are visitor number <b>{self.counter:,}</b><br>"
            f"This page last updated: {self.last_updated}<br>"
            f'<a href="/guestbook/index.html">Sign my guestbook</a> | '
            f'<a href="mailto:webmaster@localhost">Email the webmaster</a>'
            "<br>Best viewed with Netscape Navigator 4.0 at 800x600</font>"
            "</td></tr></table></center>"
        )


@register
class Hybrid(Archive):
    name = "hybrid"

    def transform(self, req, ctype: str, body: bytes) -> bytes:
        if "text/html" not in (ctype or ""):
            return body
        try:
            text = body.decode(errors="replace")
        except Exception:
            return body

        if "</body>" in text.lower():
            idx = text.lower().rindex("</body>")
            chrome = _Chrome(req.host or "", req.path,
                             req.profile.label if req.profile else "?")
            text = text[:idx] + chrome.footer_html() + text[idx:]
        # strip any wayback-era absolute https that slipped through
        text = text.replace("https://", "http://")
        return text.encode(errors="replace")
