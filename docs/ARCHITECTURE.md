# Dialback — Architecture

This document describes the high-level design of Dialback: a Raspberry Pi appliance
that gives retro computers period-appropriate internet access, blending the Wayback
Machine with LLM-synthesized content.

## 1. Network topology

The Pi connects to the real internet over WiFi and serves the retro PC over ethernet
(or serial for pre-ethernet machines):

```
[Internet] ──wifi──> [Raspberry Pi] ──ethernet──> [Retro PC]
                          │
                          ├── wlan0: uplink (wpa_supplicant, DHCP client)
                          ├── eth0: downstream (static IP, DHCP server via dnsmasq)
                          └── serial: SLIP/PPP daemon (planned) — emulates a dial-up ISP
```

### Ethernet path

- Pi runs **dnsmasq** on `eth0` for DHCP + DNS.
- IP forwarding + NAT via **nftables** routes downstream traffic out through `wlan0`.
- The retro PC experiences "full internet" — except every request is intercepted.

### Serial path (later phase)

- `pppd` over a USB/UART serial line; SLIP for very old TCP/IP stacks.
- The retro PC "dials in" to the Pi as if it were an era-appropriate ISP.
- Design constraint: a connection is an abstract client with metadata
  (link type, machine type) that routing rules may key off in future.

## 2. Interception layer

All interception happens at the OS level so the service stays replaceable:

- **DNS:** dnsmasq resolves every name to the Pi itself. Any hostname the retro PC
  looks up lands on Dialback.
- **Transparent redirect (nftables):**
  - Ports 80 (and any plain HTTP traffic) → router's HTTP handler.
  - HTTPS from retro browsers is mostly moot (era browsers lack TLS); when present,
    it is redirected to a handler that can serve period-authentic errors or be
    bridged later.
  - All other ports → protocol dispatcher (stub interface now; this is where future
    bridges like AIM → Discord plug in).
- TLS on the *uplink* side is normal: the Pi itself makes outbound requests to
  archive.org and LLM APIs; the retro PC only ever speaks plain HTTP to the Pi.

## 3. Core services

Dialback runs as one supervisor process with internal modules (start monolithic,
split into separate processes if profiling demands it).

### 3.1 Dialback Router (the heart)

Every intercepted request is normalized into an internal request object:

```
Request {
  url / host / port / protocol
  client_info   // link type (ethernet vs serial), user-agent → era hints
  era           // resolved from global setting + per-domain overrides
}
```

The router evaluates a rule chain against this object:

```yaml
rules:
  - match: { host: "*.geocities.com" }
    provider: archive            # real archived pages
  - match: { host: "www.altavista.com" }
    provider: hybrid             # real snapshot, synthesized filler
  - match: { port: 5190 }        # future: AIM bridge
    provider: discord-bridge
  - default:
    provider: llm                # invented site
```

Responsibilities:

- **Rule matching** by domain, port, protocol, and other request attributes.
- **Provider dispatch**: providers implement `handle(request) -> response`.
  Built-ins: `archive`, `llm`, `hybrid`. Providers are plugins; new backends are
  drop-in.
- **Era resolution**: the router decides which era applies to each request
  (global dial setting + per-domain pinning).

### 3.2 Era Engine

A single declarative source of truth consumed by all providers and post-processors:

```yaml
era: 1997-06
protocols: [http]               # e.g. https attempts yield period-authentic errors
page_style:                     # hints for LLM generation + post-processing
  max_width: 640
  colors: websafe
  features: [tables, frames, guestbook, hit-counter, under-construction-gifs]
search_engines: [altavista, yahoo-directory]
```

- Ship **year presets** (1994, 1996, 1998, 2001, …) but allow arbitrary date dialing.
- Both the archive provider's CDX queries and the LLM prompts derive from the same
  era profile, keeping archived and synthesized content mutually consistent.
- Per-domain overrides let specific sites stay pinned to a fixed year regardless of
  the global dial.

### 3.3 Providers

#### Archive provider

- Queries the **Wayback CDX API** for the snapshot closest to the target date.
- Rewrites links inside fetched pages so navigation stays inside Dialback and stays
  within the era window (snapshots outside the window are filtered/re-resolved).
- Aggressive local caching (see §3.5).

#### LLM provider

- Generates plausible sites/pages on demand: given `(host, path, era)`, prompts an
  LLM using the era profile and synthesizes HTML.
- **De-anachronizer post-pass**: deterministic template wrapping + filtering strips
  modern idioms and enforces era page style (inline CSS only, table layouts, etc.)
  so output is visually consistent regardless of model quirks.
- **Deterministic-ish generation**: seed from `(host, era)` so revisiting "the same"
  site is stable within a session.
- **Pluggable backend**: API-based providers first (the Pi cannot run capable local
  models); interface leaves room for small local models later.
- **Cost control**: per-session / daily budget caps belong here from day one.

#### Hybrid provider

- Real archived index/content where available; synthesized filler (guestbooks,
  dead links, badges, comment sections) where the archive is thin.

### 3.4 Protocol dispatcher (stub)

Non-HTTP ports route through a dispatcher interface reserved for future protocol
bridges (AIM → Discord being the motivating example). Nothing lives here yet; the
interface exists so interception rules can already reference it.

### 3.5 Cache layer

- One shared disk cache keyed by **`(host, path, resolved_era)`**, with era at coarse
  granularity (month or preset-bucket) so entries actually hit across visits.
- Content-addressed storage under `/var/cache/dialback/`.
- **Eviction policy: fill-first.** Fill toward a disk threshold (~85% of SD), then
  evict least-recently-used. No cleverness needed.
- LLM generations cache identically — regenerating fake Geocities pages on every
  visit wastes time and money.
- Admin UI exposes usage stats and purge-by-site / purge-by-era actions.

### 3.6 Control plane / web UI

Served by the Pi on its own admin address (e.g. `admin.dialback`, reachable from a
modern laptop too — not only through the proxy):

- Date dial / year presets — flip eras at runtime
- Rule editor (domain/provider routing), YAML-backed and validated
- Live request log ("retro PC asked for X → served by archive, cached")
- Status: uplink health, connected clients, cache usage, LLM spend vs. budget

## 4. Repo layout

```
dialback/
├── README.md
├── docs/
│   └── ARCHITECTURE.md
├── install/
│   ├── bootstrap.sh          # stock PiOS Lite → working appliance
│   └── rootfs-overlays/      # dnsmasq.conf, nftables rules, systemd units
├── src/
│   ├── gateway/              # interception glue (mostly config, some code)
│   ├── router/               # rule engine, era resolution, request objects
│   ├── providers/
│   │   ├── archive/
│   │   ├── llm/
│   │   └── hybrid/
│   ├── era/                  # era profiles + presets
│   ├── cache/
│   └── control/              # web UI + REST API
└── eras/                     # user-editable era definitions
```

## 5. OS strategy

Dialback installs **on top of stock Raspberry Pi OS Lite (64-bit)** rather than
building a custom OS image:

- Flash official OS with the imager, run `install/bootstrap.sh`, done.
- Standard networking tools (dnsmasq, nftables, pppd) stay debuggable and documented.
- Normal package ecosystem access for everything else.

A single bootstrap script (or Ansible later, if it outgrows a script) provides the
appliance feel without the maintenance burden of custom image building.

## 6. Build order

Each stage produces something demoable:

| # | Stage | Demo |
|---|-------|------|
| 1 | Gateway | Retro PC browses the *real* internet through the Pi (NAT over wifi→ethernet) |
| 2 | Interception | Every request hits the router; placeholder page served |
| 3 | Era engine + archive provider | Pick 1997, browse real archived pages |
| 4 | Cache | Instant repeat visits; tolerant of uplink hiccups |
| 5 | LLM provider | Unknown hosts get invented sites |
| 6 | Control UI | Date dialing at runtime |
| 7 | Hybrid + polish | Blended pages; then serial/PPP as its own mini-project |

## 7. Risks & open questions

- **HTTPS:** retro browsers mostly can't do TLS — convenient, since interception is
  plain HTTP. Edge cases handled per-era (authentic error messages).
- **Wayback rate limits & flakiness:** the cache is not optional even in v1; consider
  prefetching popular domains for chosen presets.
- **SD card wear:** heavy caching writes a lot. Keep logs volatile (journald in RAM);
  support a USB SSD as the cache target down the road.
- **LLM cost control:** budget caps enforced in the provider from day one.
- **Era granularity of cache keys:** too fine (exact day) kills hit rates; too coarse
  breaks fidelity. Start at month-level and tune.
