"""Admin/control-plane UI: date dialing, status, per-domain log, cache tools.

Reachable through the interception layer via the admin hostname, and/or on
a dedicated LAN-facing port. Deliberately simple HTML (renders fine on
period browsers too).
"""
from __future__ import annotations

import datetime
import glob
import html
import os
import urllib.parse

import yaml

from .era import FLOOR_DATE, valid_date
from .request import Request

STATE_FILE = "/var/lib/dialback/state.yaml"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

PAGE_SHELL = """<!DOCTYPE HTML>
<html><head><meta charset="utf-8"><title>Dialback Control</title>
<style>
 body {{ font-family: georgia, serif; background: #1a1a2e; color: #eaeaea; margin: 0; }}
 .wrap {{ max-width: 900px; margin: 0 auto; padding: 20px; }}
 h1 {{ font-weight: normal; letter-spacing: 2px; }} h1 b {{ color: #e94560; }}
 h2 {{ border-bottom: 1px solid #444; padding-bottom: 4px; font-weight: normal;
      color: #a0a0c0; font-size: 15px; text-transform: uppercase; letter-spacing: 1px;
      margin-top: 28px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
 td, th {{ padding: 5px 8px; border-bottom: 1px solid #333; text-align: left; }}
 th {{ color: #8888aa; font-weight: normal; }}
 .big {{ font-size: 42px; color: #e94560; }}
 .muted {{ color: #777799; }}
 select {{ background: #2a2a44; color: #eaeaea; border: 1px solid #555;
          padding: 4px 6px; font-size: 15px; }}
 button {{ background: #e94560; color: white; border: 0; padding: 8px 22px;
          font-size: 15px; cursor: pointer; }}
 button.minor {{ background: #444466; padding: 4px 12px; font-size: 13px; }}
 .flash {{ background: #2a4d3a; padding: 8px 12px; margin: 10px 0; }}
 label {{ display: block; margin: 3px 0; }}
</style></head>
<body><div class="wrap">
<h1><b>DIAL</b>BACK <span class="muted">control</span></h1>
{body}
</div></body></html>"""


def _fmt_bytes(n) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class AdminApp:
    def __init__(self, engine, traffic, admin_cfg: dict | None = None,
                 crawler=None):
        self.engine = engine
        self.traffic = traffic          # TrafficLog
        self.crawler = crawler          # Crawler or None
        cfg = admin_cfg or {}
        self.hostname = cfg.get("hostname", "admin.dialback")
        self.started_at = datetime.datetime.now()

    # ----------------------------------------------------------- routing --

    async def handle(self, req: Request) -> None:
        path = req.path.split("?")[0].rstrip("/") or "/admin"
        if req.method == "POST":
            form = await self._read_form(req)
            if path == "/admin/era":
                wanted = (form.get("era") or [""])[0]
                if wanted in self.available_eras():
                    self.engine.switch_era(wanted)
                    self._persist_state({"era": wanted})
                    await self._redirect(req, flash=f"Era set to {wanted}.")
                else:
                    await self._redirect(req, flash=f"Unknown era '{wanted}'.")
            elif path == "/admin/date":
                iso = self._date_from_form(form)
                if iso:
                    self.engine.set_date(iso)
                    self._persist_state({"date": iso})
                    await self._redirect(req, flash=f"Time set to {iso}.")
                else:
                    await self._redirect(
                        req, flash="Invalid date (must be between "
                                   f"{FLOOR_DATE} and today).")
            elif path == "/admin/crawl":
                host = (form.get("host") or [""])[0]
                max_pages = (form.get("max_pages") or ["30"])[0]
                if self.crawler is None:
                    await self._redirect(req, flash="Crawler unavailable.")
                    return
                ok, msg = self.crawler.start(host, max_pages)
                await self._redirect(req, flash=msg)
            elif path == "/admin/crawl/cancel":
                if self.crawler and self.crawler.cancel():
                    await self._redirect(req, flash="Crawl cancelled.")
                else:
                    await self._redirect(req, flash="No crawl to cancel.")
            elif path == "/admin/cache/purge":
                cache = self._cache()
                if cache is not None:
                    freed = cache.purge()
                    await self._redirect(req,
                                         flash=f"Cache purged ({_fmt_bytes(freed)}).")
                else:
                    await self._redirect(req, flash="No cache provider found.")
            else:
                await self._serve_page(req, "<p>Unknown action.</p>", 404)
            return

        if path == "/admin/api/status":
            import json
            body = json.dumps(self._status()).encode()
            head = ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n").encode()
            req.writer.write(head + body)
            await req.writer.drain()
            return

        if path == "/admin":
            flash = urllib.parse.parse_qs(
                urllib.parse.urlparse(req.path).query).get("flash", [""])[0]
            await self._serve_dashboard(req, flash=flash)
            return

        await self._serve_page(req, "<p>Unknown admin path.</p>", 404)

    async def _read_form(self, req: Request) -> dict:
        form_raw = b""
        length = 0
        for line in req.raw_head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip() or 0)
        if length and req.reader is not None:
            form_raw = await req.reader.readexactly(length)
        return urllib.parse.parse_qs(form_raw.decode(errors="replace"))

    @staticmethod
    def _date_from_form(form: dict) -> str | None:
        try:
            y = int((form.get("year") or [""])[0])
            m = int((form.get("month") or [""])[0])
            d = int((form.get("day") or [""])[0])
            iso = f"{y:04d}-{m:02d}-{d:02d}"
            return iso if valid_date(iso) else None
        except ValueError:
            return None

    # ------------------------------------------------------------ helpers --

    def available_eras(self) -> list[str]:
        era_dir = self.engine.era_dir
        if not era_dir:
            return [self.engine.era]
        return sorted(os.path.basename(p)[:-5]
                      for p in glob.glob(f"{era_dir}/*.yaml"))

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
        location = f"/admin?flash={urllib.parse.quote(flash)}"
        head = (f"HTTP/1.1 303 See Other\r\nLocation: {location}\r\n"
                f"Content-Length: 0\r\nConnection: close\r\n\r\n").encode()
        req.writer.write(head)
        await req.writer.drain()

    async def _serve_page(self, req: Request, body_html: str, status: int = 200,
                          flash: str = "") -> None:
        if flash:
            body_html = f'<div class="flash">{html.escape(flash)}</div>' + body_html
        body = PAGE_SHELL.format(body=body_html, refresh="").encode()
        reason = "OK" if status == 200 else "Not Found"
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        req.writer.write(head + body)
        await req.writer.drain()

    # ---------------------------------------------------------- dashboard --

    async def _serve_dashboard(self, req: Request, flash: str = "") -> None:
        parts = []
        current = self.engine.era
        prof = self.engine.profile

        parts.append("<h2>Time dial</h2>")
        parts.append('<div class="big">' + html.escape(current) + "</div>")
        if prof:
            parts.append('<p class="muted">archive window '
                         f"{prof.raw.get('start_date', '?')} &rarr; "
                         f"{prof.raw.get('end_date', '?')}</p>")

        # preset eras
        parts.append('<form method="POST" action="/admin/era">')
        for era in self.available_eras():
            checked = " checked" if era == current else ""
            parts.append(f"<label><input type=\"radio\" name=\"era\" "
                         f"value=\"{html.escape(era)}\"{checked}>"
                         f"{html.escape(era)} preset</label>")
        parts.append('<button type="submit" class="minor">Dial preset</button></form>')

        # free-date picker: three selects, IE-friendly
        today = datetime.date.today()
        floor = datetime.date.fromisoformat(FLOOR_DATE)
        years = list(range(floor.year, today.year + 1))
        try:
            sel = datetime.date.fromisoformat(current)
            sy, sm, sd = sel.year, sel.month, sel.day
        except ValueError:
            sy = sm = sd = None
        parts.append('<form method="POST" action="/admin/date">')
        parts.append("<select name=\"year\">")
        for y in years:
            s = " selected" if y == sy else ""
            parts.append(f"<option{s}>{y}</option>")
        parts.append("</select>")
        parts.append("<select name=\"month\">")
        for i, name in enumerate(MONTHS, 1):
            s = " selected" if i == sm else ""
            parts.append(f"<option value=\"{i}\"{s}>{name}</option>")
        parts.append("</select>")
        parts.append("<select name=\"day\">")
        for d in range(1, 32):
            s = " selected" if d == sd else ""
            parts.append(f"<option{s}>{d}</option>")
        parts.append("</select>")
        parts.append('<button type="submit">Dial exact date</button></form>')
        parts.append(f'<p class="muted">Any date from {FLOOR_DATE} to today. '
                     "You will see the web as it existed that day - never "
                     "from the future.</p>")

        # --- status ---
        st = self._status()
        uptime = st["uptime_seconds"]
        parts.append("<h2>Status</h2><table>")
        parts.append(f"<tr><td>Uptime</td><td>{uptime//3600}h "
                     f"{(uptime%3600)//60}m</td></tr>")
        parts.append(f"<tr><td>Default provider</td><td>"
                     f"{html.escape(st['default_provider'])}</td></tr>")
        if st.get("cache"):
            c = st["cache"]
            pct = (100 * c["bytes"] / c["budget"]) if c.get("budget") else 0
            parts.append(f"<tr><td>Cache</td><td>{_fmt_bytes(c['bytes'])} in "
                         f"{c['files']} files ({pct:.0f}% of budget)</td></tr>")
        parts.append("</table>")

        # --- rules ---
        parts.append("<h2>Routing rules</h2><table><tr><th>Match</th>"
                     "<th>Provider</th></tr>")
        for r in self.engine.rules:
            pat = html.escape(r.host) if r.host else "<i>(anything)</i>"
            parts.append(f"<tr><td>{pat}</td>"
                         f"<td>{html.escape(r.provider)}</td></tr>")
        parts.append(f"<tr><td><i>(default)</i></td><td>"
                     f"{html.escape(self.engine.default_provider)}</td></tr></table>")

        # --- recently served domains ---
        parts.append("<h2>Recently served sites</h2><table><tr><th>Site</th>"
                     "<th>Provider</th><th>Hits</th><th>Last</th><th>Era</th></tr>")
        entries = reversed(list(self.traffic.hosts.items()))  # newest first
        shown = 0
        for host, info in entries:
            parts.append(
                f"<tr><td>{html.escape(host)}</td>"
                f"<td>{html.escape(info['provider'])}</td>"
                f"<td>{info['count']}</td>"
                f"<td class='muted'>{info['last']}</td>"
                f"<td class='muted'>{html.escape(info['era'])}</td></tr>")
            shown += 1
            if shown >= 25:
                break
        if not shown:
            parts.append("<tr><td colspan='5' class='muted'>"
                         "Nothing served yet.</td></tr>")
        parts.append("</table>")

        # --- cache warmer / site crawler ---
        parts.append('<h2>Cache warmer</h2>')
        parts.append('<p class="muted">Walk a whole site in the current era '
                     'and cache every page + image, so browsing it later is '
                     'instant. Stays on the domain and its own subdomains.</p>')
        job = self.crawler.status() if self.crawler else None
        if job:
            parts.append("<table><tr><th colspan=2>Crawl: "
                         f"{html.escape(job['host'])} <span class='muted'>"
                         f"({html.escape(job['status'])})</span></th></tr>")
            for label, key in (("Pages fetched", "pages"), ("OK", "ok"),
                               ("Not archived", "miss"), ("Errors", "errors"),
                               ("Assets cached", "assets"), ("Queued", "queued")):
                parts.append(f"<tr><td>{label}</td>"
                             f"<td>{job.get(key, 0)}</td></tr>")
            parts.append(f"<tr><td>Current</td><td class='muted'>"
                         f"{html.escape(str(job.get('current', '')))}</td></tr>")
            parts.append("</table>")
            if job["status"] == "running":
                parts.append('<form method="POST" action="/admin/crawl/cancel">'
                             '<button class="minor" type="submit">Cancel crawl'
                             "</button></form>")
        parts.append('<form method="POST" action="/admin/crawl">'
                     'Site: <input type="text" name="host" placeholder="geocities.com" '
                     'size="28"> &nbsp; Max pages: '
                     '<input type="text" name="max_pages" value="30" size="4"> '
                     '<button type="submit">Start crawl</button></form>')

        # --- maintenance ---
        parts.append('<h2>Maintenance</h2>'
                     '<form method="POST" action="/admin/cache/purge">'
                     '<button class="minor" type="submit">Purge cache'
                     "</button></form>")

        await self._serve_page(req, "".join(parts), flash=flash)
