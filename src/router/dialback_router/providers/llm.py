"""LLM synthesis provider: invents era-authentic sites via OpenRouter.

Narrow scope (stage 5): used as the *fallback* for hosts that have no
archived snapshot. Generation is cached per (era, host, path) so revisits
are stable and cheap.
"""
from __future__ import annotations

import asyncio
import datetime
import html
import json
import logging
import os
import re
import urllib.request

from .base import Provider, register
from ..cache import DiskCache
from ..request import Request
from ..era import EraProfile

log = logging.getLogger("dialback.llm")

DEFAULT_MODEL = "gpt-5.6-luna"
API_BASE = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT = 120
REQUEST_RETRIES = 1

SECRETS_FILE = "/etc/dialback/secrets/openrouter.key"


def read_key() -> str | None:
    """Key from env var or secrets file. Tolerates VAR=value file format."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        try:
            with open(SECRETS_FILE) as f:
                key = f.read().strip()
        except OSError:
            return None
    key = key.strip()
    if key.startswith("OPENROUTER_API_KEY="):
        key = key.split("=", 1)[1].strip()
    return key or None


def _post_chat(api_key: str, base: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def strip_anachronisms(text: str) -> str:
    """Light de-anachronizer: things era browsers shouldn't see."""
    text = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", text)
    text = re.sub(r"(?is)<script\b[^>]*/>", "", text)
    return text.replace("https://", "http://")


@register
class LLM(Provider):
    name = "llm"

    def __init__(self, options: dict | None = None):
        super().__init__(options)
        self.model = self.options.get("model", DEFAULT_MODEL)
        self.api_base = self.options.get("api_base", API_BASE)
        self.max_tokens = int(self.options.get("max_tokens", 4000))
        self.daily_cap = int(self.options.get("daily_request_cap", 0) or 0)
        self.cache = DiskCache(self.options.get("cache_root", "/var/cache/dialback"))
        self._counter_day: str | None = None
        self._counter_value = 0

    # ------------------------------------------------------------- main --

    async def handle(self, req: Request) -> None:
        if not req.host or not req.profile:
            await self._serve_page(req, "Bad Request",
                                   "No host or era for this request.", 400)
            return

        ident = f"{req.profile.label}|{req.host}{req.path}"
        cached = self.cache.get("llm", ident)
        if cached:
            ctype, _, body = cached.partition(b"\n")
            await self._serve(req, 200, ctype.decode(errors="replace") or "text/html",
                              body, head_only=(req.method == "HEAD"))
            return

        api_key = read_key()
        if not api_key:
            await self._serve_page(
                req, "The Oracle is Silent",
                "This site was never archived, and site synthesis is not "
                "configured (no OpenRouter API key).", 503)
            return
        if self._over_daily_cap():
            await self._serve_page(
                req, "Synthesis Quota Reached",
                "The daily synthesis quota has been used up. "
                "Come back tomorrow.", 429)
            return

        system_prompt = self._system_prompt(req.profile)
        user_prompt = self._user_prompt(req.profile, req.host, req.path)

        content, last_err = None, None
        for attempt in range(REQUEST_RETRIES + 1):
            try:
                _, content = await asyncio.to_thread(
                    self._generate, api_key, system_prompt, user_prompt
                )
                break
            except (OSError, TimeoutError, KeyError, ValueError) as e:
                last_err = e
                log.warning("generation attempt %d failed: %s", attempt + 1, e)
                if attempt < REQUEST_RETRIES:
                    await asyncio.sleep(2)

        if content is None:
            await self._serve_page(
                req, "Synthesis Failed",
                "This site could not be invented right now. "
                f"({html.escape(str(last_err))})", 502)
            return

        body = strip_anachronisms(content).encode(errors="replace")
        self.cache.put("llm", ident, b"text/html\n" + body)
        log.info("synthesized %s [%s]", ident, self.model)
        await self._serve(req, 200, "text/html", body,
                          head_only=(req.method == "HEAD"))

    # -------------------------------------------------------- generation --

    def _generate(self, api_key: str, system_prompt: str, user_prompt: str):
        """Blocking OpenRouter call. Returns (usage, html_content)."""
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        data = _post_chat(api_key, self.api_base, payload, REQUEST_TIMEOUT)
        usage = data.get("usage", {})
        content = data["choices"][0]["message"]["content"]
        # models sometimes wrap output in markdown fences; strip them
        content = re.sub(r"^```(?:html)?\s*\n?", "", content.strip())
        content = re.sub(r"\n?```\s*$", "", content.strip())
        self._bump_counter()
        return usage, content

    def _system_prompt(self, prof: EraProfile) -> str:
        year = prof.name
        style = prof.raw.get("page_style", {})
        features = ", ".join(style.get("features", [])) or "table layouts"
        engines = ", ".join(prof.raw.get("search_engines", []))
        return (
            f"You are a webmaster building websites for the internet of "
            f"{year}. You produce COMPLETE, SELF-CONTAINED HTML pages that "
            "look exactly like real pages from that time.\n"
            f"Era-appropriate design features to use: {features}.\n"
            f"Websites of this era commonly reference: {engines}.\n"
            "STRICT RULES:\n"
            "- Single HTML file; all styling inline or in a <style> block\n"
            "- NO javascript, NO external stylesheets, NO images from other "
            "domains (use simple colored table cells instead of photos)\n"
            "- http:// URLs only (never https)\n"
            "- Layout with nested tables, bgcolor and font tags - not modern CSS\n"
            "- Content, slang, technology references, copyright dates and "
            "'best viewed in' badges must be period-perfect\n"
            "- Links may point anywhere on the same site (relative paths) or "
            "to other contemporary sites\n"
            "- Output ONLY raw HTML, starting with <!DOCTYPE or <html>"
        )

    def _user_prompt(self, prof: EraProfile, host: str, path: str) -> str:
        if path in ("", "/"):
            ask = ("Build this site's homepage. Invent what this domain "
                   "plausibly was in that era - a business, fan page, portal, "
                   "or personal homepage. Make it feel genuinely lived-in: "
                   "visitor counter, guestbook link, webring navigation, "
                   "last-updated date, webmaster email link.")
        else:
            ask = (f"Build the page at this path within the site '{host}', "
                   "consistent with what its homepage would have looked like.")
        return f"It is {prof.name}. The URL is http://{host}{path}\n{ask}"

    # ------------------------------------------------------------- quota --

    def _over_daily_cap(self) -> bool:
        if not self.daily_cap:
            return False
        today = datetime.date.today().isoformat()
        if today != self._counter_day:
            self._counter_day = today
            self._counter_value = 0
        return self._counter_value >= self.daily_cap

    def _bump_counter(self) -> None:
        self._counter_value += 1
