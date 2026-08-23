"""Provider base class and registry."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..request import Request

_REGISTRY: dict[str, type["Provider"]] = {}
_CONFIG: dict[str, dict] = {}          # per-provider config from dialback.yaml


def set_provider_config(config: dict[str, dict]) -> None:
    """Install the `providers:` section so fallback-created providers get options."""
    global _CONFIG
    _CONFIG = config or {}


def register(cls: type["Provider"]) -> type["Provider"]:
    """Class decorator: adds a Provider subclass to the registry by its `name`."""
    _REGISTRY[cls.name] = cls
    return cls


def create(name: str, options: dict | None = None) -> "Provider":
    if name not in _REGISTRY:
        raise KeyError(f"unknown provider '{name}' (known: {sorted(_REGISTRY)})")
    if options is None:
        options = _CONFIG.get(name, {})
    return _REGISTRY[name](options)


def known() -> list[str]:
    return sorted(_REGISTRY)


class Provider(ABC):
    """A provider fulfills intercepted requests.

    Subclasses must set `name` and implement handle(). Options come from the
    `providers:` section of dialback.yaml.
    """

    name: str = ""

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    @abstractmethod
    async def handle(self, req: Request) -> None:
        """Fulfill the request by writing a response to req.writer."""

    # ------------------------------------------------ shared response helpers

    async def _serve(self, req: Request, status: int, ctype: str, body: bytes,
                     head_only: bool = False) -> None:
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden",
                  404: "Not Found", 429: "Too Many Requests",
                  500: "Internal Server Error", 502: "Bad Gateway",
                  503: "Service Unavailable"}.get(status, "OK")
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
        import html as _html
        h = _html.escape
        era = getattr(req.profile, "label", "?") if req.profile else "?"
        body = f'''<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><meta charset="utf-8"><title>Dialback - {h(title)}</title></head>
<body bgcolor="#c0c0c0"><center>
<table width="600" cellpadding="10" border="2" bgcolor="#ffffff">
<tr><td bgcolor="#000080"><font color="#ffffff" size="5"><b>Dialback</b></font></td></tr>
<tr><td><h3>{h(title)}</h3>
<p>{message}</p>
<p><i>Era: {h(era)}</i></p>
</td></tr></table></center></body></html>'''.encode()
        await self._serve(req, status, "text/html", body)
