"""Placeholder provider: proves interception works; serves an era-stamped page.

Replaced in later stages by archive/LLM/hybrid providers.
"""
from __future__ import annotations

import html

from .base import Provider, register
from ..request import Request


@register
class Placeholder(Provider):
    name = "placeholder"

    async def handle(self, req: Request) -> None:
        h = lambda s: html.escape(s or "")  # noqa: E731
        orig = f"{req.orig_dst[0]}:{req.orig_dst[1]}" if req.orig_dst else "unknown"
        body = f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html>
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=utf-8">
  <title>Dialback &mdash; {h(req.host)}</title>
</head>
<body bgcolor="#c0c0c0" text="#000000" link="#0000ee">
<center>
<table width="600" cellpadding="10" border="2" cellspacing="0" bgcolor="#ffffff">
<tr><td bgcolor="#000080"><font color="#ffffff" size="5"><b>Dialback</b></font></td></tr>
<tr><td>
<p>This request was <b>intercepted by the Dialback router</b> and served by the
<b>{h(self.name)}</b> provider.</p>
<table border="1" cellpadding="4">
<tr><td><b>Host</b></td><td>{h(req.host)}</td></tr>
<tr><td><b>Path</b></td><td>{h(req.method)} {h(req.path)}</td></tr>
<tr><td><b>Original destination</b></td><td>{h(orig)}</td></tr>
<tr><td><b>Era</b></td><td>{h(req.era)}</td></tr>
</table>
<p><i>The real internet used to be here.</i></p>
</td></tr>
</table>
</center>
</body>
</html>
""".encode()

        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        req.writer.write(head + body)
        await req.writer.drain()
