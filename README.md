# Dialback

**Dialback** is a time machine for the internet, running on a Raspberry Pi. Connect
your retro computer to it, and it provides *period-appropriate* internet access —
the web as it existed when your machine was new.

It blends two sources of "the past":

- **Archive.org (Wayback Machine)** — real pages, real snapshots, served as they
  existed at the configured point in time.
- **LLM-synthesized content** — plausible, era-authentic websites invented on demand,
  filling in the parts history never saved.

Dial a year — 1994, 1997, 2001 — and browse. Geocities pages load through table
layouts, search engines are AltaVista, unknown domains get guestbooks and
"under construction" GIFs.

## How it works

The Pi connects to your network over WiFi and acts as the retro machine's internet:

```
[Internet] ──wifi──> [Raspberry Pi running Dialback] ──ethernet──> [Retro PC]
```

- **Ethernet** (preferred): the Pi runs DHCP/DNS/NAT on its ethernet port; the retro
  PC plugs in and just works.
- **Serial / null-modem** (planned): for machines without ethernet, the Pi emulates
  a dial-up ISP over SLIP/PPP.

All traffic from the retro PC passes through a transparent interception layer. A
central **router** decides how each request is fulfilled — archived snapshot,
synthesized site, or (later) protocol bridges like AIM → Discord.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design.

## Features

- Transparent proxying: no configuration needed on the retro PC beyond standard
  network setup of its era
- Time dialing: switch eras at runtime from a simple browser-reachable control page
- Era engine: one declarative config drives archive filtering, LLM generation, and
  page post-processing (protocols available, layout style, feature set per period)
- Pluggable providers: route requests by domain, port, or other criteria to
  archive.org, LLM synthesis, hybrid modes, or future custom backends
- Era-aware caching keyed by `(site, era)` that fills the disk before evicting

## Status

Design phase. See the architecture doc and the planned build order:

1. Gateway: Pi as NAT box (wifi uplink → ethernet downstream)
2. Interception: transparent redirect into the router service
3. Archive provider + era engine
4. Cache layer
5. LLM synthesis provider
6. Control web UI
7. Hybrid provider, serial/PPP support

## Requirements (planned)

- Raspberry Pi with WiFi + ethernet (Pi 3 or later recommended)
- Stock **Raspberry Pi OS Lite** (64-bit) — Dialback installs on top via bootstrap script
- Retro PC with ethernet, or serial port (for the eventual SLIP/PPP path)
- API access to an LLM provider for site synthesis

## License

TBD
