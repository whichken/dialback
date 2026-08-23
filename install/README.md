# Dialback — Gateway bootstrap (stage 1)

Turns a stock Raspberry Pi OS Lite installation into the Dialback network
gateway.

## Topology after bootstrap

```
[Internet] --wifi--> [Pi: wlan0] <--NAT-- [Pi: eth0 = 10.0.0.1/24] --> retro PC
```

- `wlan0`: upstream link (unchanged; stays the default route)
- `eth0`: downstream to the retro PC — static `10.0.0.1`, serves DHCP
  (`10.0.0.100–150`) and DNS via `dnsmasq`
- `nftables`: forwarding + masquerade from eth0 out wlan0
- IP forwarding enabled via `/etc/sysctl.d/90-dialback.conf`

## Usage

On a fresh Raspberry Pi OS Lite install, get this directory onto the Pi and run:

```bash
./bootstrap.sh
```

Idempotent — safe to re-run. Requires a user with passwordless sudo.

## What it installs/configures

| File | Purpose |
|------|---------|
| `/etc/dnsmasq.d/dialback.conf` | DHCP + DNS on eth0; `admin.dialback` pinned to the Pi |
| `/etc/nftables.conf` | forward + NAT masquerade rules |
| `/etc/sysctl.d/90-dialback.conf` | `net.ipv4.ip_forward=1` |

## Verified

- Simulated client (network namespace): DNS via gateway + HTTP through NAT ✓
- True end-to-end test requires plugging an actual client into eth0.
