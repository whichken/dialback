"""Admin/control-plane UI: era dialing, status, request log, cache tools.

Reachable through the interception layer via the admin hostname, and/or on
a dedicated LAN-facing port. Deliberately simple HTML (renders fine on
period browsers too).
"""
from __future__ import annotations

import collections
import datetime
import glob
import html
import os
import pathlib
import urllib.parse

import yaml

from .request import Request

STATE_FILE = "/var/lib/dialback/state.yaml"

PAGE_SHELL = """<!DOCTYPE HTML>
<html><head><meta charset="utf-8"><title>Dialback Control</title>
<style>
 body {{ font-family: georgia, serif; background: #1a1a2e; color: #eaeaea; margin: 0; }}
 .wrap {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
 h1 {{ font-weight: normal; letter-spacing: 2px; }} h1 b {{ color: #e94560; }}
 h2 {{ border-bottom: 1px solid #444; padding-bottom: 4px; font-weight: normal;
      color: #a0a0c0; font-size: 15px; text-transform: uppercase; letter-spacing: 1px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
 td, th {{ padding: 5px 8px; border-bottom: 1px solid #333; text-align: left; }}
 th {{ color: #8888aa; font-weight: normal; }}
 .big {{ font-size: 42px; color: #e94560; }}
 .muted {{ color: #777799; }}
 input[type=radio] {{ margin-right: 6px; }}
 button {{ background: #e94560; color: white; border: 0; padding: 8px 22px;
          font-size: 15px; cursor: pointer; }}
 button.minor {{ background: #444466; padding: 4px 12px; font-size: 13px; }}
 .flash {{ background: #2a4d3a; padding: 8px 12px; margin: 10px 0; }}
</style></head>
<body><div class="wrap">
<h1><b>DIAL</b>BACK <span class="muted">control</span></h1>
{body}
</div></body></html>"""


def _fmt_bytes(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


class AdminApp:
    def __init__(self, engine, request_log: collections.deque,
                 admin_cfg: dict | None = None):
        self.engine = engine
        self.request_log = request_log
        cfg = admin_cfg or {}
        self.hostname = cfg.get("hostname", "admin.dialback")
        self.started_at = datetime.datetime.now()

    # ----------------------------------------------------------- routing --

    async def handle(self, req: Request) -> None:
        path = req.path.rstrip("/") or "/admin"
        if req.method == "POST":
            form_raw = b""
            if req.reader is not None:
                length = 0
                for line in req.raw_head.split(b"\r\n")[1:]:
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip() or 0)
                if length:
                    form_raw = await req.reader.readexactly(length)
            form = urllib.parse.parse_qs(form_raw.decode(errors="replace"))
            if path == "/admin/era":
                await self._post_era(req, form)
                return
            if path == "/admin/cache/purge":
                await self._post_cache_purge(req)
                return

        if path == "/admin/api/status":
            import json
            body = json.dumps(self._status()).encode()
            head = (f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
            req.writer.write(head + body)
            await req.writer.drain()
            return

        if path in ("/admin", "/admin/log"):
            await self._serve_dashboard(req)
            return

        await self._serve_page(req, "<p>Unknown admin path.</p>", 404)

    async def _post_era(self, req: Request, form: dict) -> None:
        wanted = (form.get("era") or [""])[0]
        eras = self.available_eras()
        if wanted in eras:
            self.engine.switch_era(wanted)
            self._persist_state({"era": wanted})
            await self._redirect(req, flash=f"Era set to {wanted}.")
        else:
            await self._redirect(req, flash=f"Unknown era '{html.escape(wanted)}'.")

    async def _post_cache_purge(self, req: Request, ) -> None:
        cache = self._cache()
        freed = None
        if cache is not None:
            freed = cache.purge()
        await self._redirect(req, flash="Cache purged." if freed is not None
                             else "No cache provider found.")

    # ------------------------------------------------------------ helpers --

    def available_eras(self) -> list[str]:
        era_dir = self.engine.era_dir
        if not era_dir:
            return [self.engine.era]
        return sorted(
            os.path.basename(p)[:-5] for p in glob.glob(f"{era_dir}/*.yaml")
        )

    def _cache(self):
        inst = self.engine._instances.get("archive")
        return getattr(inst, "cache", None) if inst else None

    def _persist_state(self, state: dict) -> None:
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w") as f:
                yaml.safe_dump(state, f)
        except OSError:
            pass

    @staticmethod
    def load_state() -> dict:
        try:
            with open(STATE_FILE) as f:
                return yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            return {}

    def _status(self) -> dict:
        cache = self._cache()
        return {
            "era": self.engine.era,
            "default_provider": self.engine.default_provider,
            "uptime_seconds": int((datetime.datetime.now()
                                   - self.started_at).total_seconds()),
            "cache": cache.stats() if cache else None,
        }

    # ------------------------------------------------------------- pages --

    async def _redirect(self, req: Request, flash: str) -> None:
        sep = "&" if "?" in "/admin?" else "?"
        location = f"/admin{sep}flash={urllib.parse.quote(flash)}"
        head = (f"HTTP/1.1 303 See Other\r\nLocation: {location}\r\n"
                f"Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
        req.writer.write(head)
        await req.writer.drain()

    async def _serve_page(self, req: Request, body_html: str, status: int = 200,
                          flash: str = "") -> None:
        if flash:
            body_html = f'<div class="flash">{html.escape(flash)}</div>' + body_html
        body = PAGE_SHELL.format(body=body_html).encode()
        reason = "OK" if status == 200 else "Not Found"
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        req.writer.write(head + body)
        await req.writer.drain()

    async def _serve_dashboard(self, req: Request) -> None:
        flash = ""
        if "flash=" in req.path:
            flash = urllib.parse.parse_qs(
                urllib.parse.urlparse(req.path).query).get("flash", [""])[0]

        prof = self.engine.profile
        parts = []

        # --- era dial ---
        current = self.engine.era
        parts.append("<h2>Time dial</h2>")
        parts.append('<div class="big">' + html.escape(current) + "</div>")
        if prof:
            parts.append(f'<p class="muted">window {prof.raw.get("start_date")} '
                         f"&rarr; {prof.raw.get('end_date')}</p>")
        parts.append('<form method="POST" action="/admin/era">')
        for era in self.available_eras():
            checked = " checked" if era == current else ""
            parts.append(f'<label style="display:block;margin:4px 0">'
                         f'<input type="radio" name="era" value="{html.escape(era)}"'
                         f"{checked}>{html.escape(era)}</label>")
        parts.append('<button type="submit">Dial</button></form>')

        # --- status ---
        parts.append("<h2>Status</h2><table>")
        st = self._status()
        uptime = st["uptime_seconds"]
        parts.append(f"<tr><td>Uptime</td><td>{uptime//3600}h {(uptime%3600)//60}m</td></tr>")
        parts.append(f"<tr><td>Default provider</td><td>"
                     f"{html.escape(st['default_provider'])}</td></tr>")
        if st.get("cache"):
            c = st["cache"]
            pct = (100 * c["bytes"] / c["budget"]) if c.get("budget") else 0
            parts.append(f"<tr><td>Cache</td><td>{_fmt_bytes(c['bytes'])} in "
                         f"{c['files']} files ({pct:.0f}% of budget)</td></tr>")
        parts.append("</table>")

        # --- rules ---
        parts.append("<h2>Routing rules</h2><table><tr><th>Match</th><th>Provider</th></tr>")
        for r in self.engine.rules:
            pat = html.escape(r.host) if r.host else "<i>(anything)</i>"
            parts.append(f"<tr><td>{pat}</td><td>{html.escape(r.provider)}</td></tr>")
        parts.append(f"<tr><td><i>(default)</i></td><td>"
                     f"{html.escape(self.engine.default_provider)}</td></tr></table>")

        # --- cache tools ---
        parts.append('<h2>Maintenance</h2>'
                     '<form method="POST" action="/admin/cache/purge">'
                     '<button class="minor" type="submit">Purge cache</button></form>')

        # --- request log ---
        parts.append("<h2>Recent requests</h2><table><tr>"
                     "<th>Time</th><th>Client</th><th>Provider</th>"
                     "<th>Request</th><th>Era</th></tr>")
        for entry in reversed(list(self.request_log)[-50:]):
            parts.append(
                "<tr>"
                f"<td class='muted'>{entry['time']}</td>"
                f"<td>{html.escape(entry['client'])}</td>"
                f"<td>{html.escape(entry['provider'])}</td>"
                f"<td>{html.escape(entry['method'])} http://"
                f"{html.escape(entry['host'])}{html.escape(entry['path'])}</td>"
                f"<td class='muted'>{html.escape(entry['era'])}</td>"
                "</tr>")
        parts.append("</table>")

        await self._serve_page(req, "".join(parts), flash=flash)
