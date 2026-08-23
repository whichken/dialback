#!/usr/bin/env bash
#
# Dialback gateway bootstrap.
#
# Run this ON the Raspberry Pi (as a user with passwordless sudo) after flashing
# stock Raspberry Pi OS Lite. It turns the Pi into the Dialback network gateway:
#
#   eth0   -> downstream to the retro PC: static IP + DHCP/DNS via dnsmasq
#   wlan0  -> upstream internet link: NAT/masquerade via nftables
#
# Idempotent: safe to run repeatedly.

set -euo pipefail

# sbin tools (nmcli, sysctl, nft) are not always on a non-login SSH PATH
export PATH="/usr/local/sbin:/usr/sbin:/sbin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OVERLAYS="$SCRIPT_DIR/rootfs-overlays"

GATEWAY_IP="10.0.0.1"
GATEWAY_CIDR="$GATEWAY_IP/24"
# NetworkManager connection name that manages eth0 (created by first-boot setup)
ETH_CON="netplan-eth0"

log() { echo "[dialback] $*"; }

[[ $(id -u) -eq 0 ]] && { echo "Run as a normal user with sudo, not as root." >&2; exit 1; }
[[ -d "$OVERLAYS" ]] || { echo "Overlays not found at $OVERLAYS" >&2; exit 1; }

log "installing packages..."
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq dnsmasq nftables python3-yaml >/dev/null

log "copying rootfs overlays..."
sudo install -D -m 644 "$OVERLAYS/etc/sysctl.d/90-dialback.conf"     /etc/sysctl.d/90-dialback.conf
sudo install      -m 644 "$OVERLAYS/etc/nftables.conf"              /etc/nftables.conf
sudo install -D -m 644 "$OVERLAYS/etc/dnsmasq.d/dialback.conf"      /etc/dnsmasq.d/dialback.conf

log "enabling IP forwarding..."
sudo sysctl --system >/dev/null
sysctl -n net.ipv4.ip_forward | grep -q '^1$' || { echo "IP forwarding failed to enable" >&2; exit 1; }

log "configuring $ETH_CON as downstream gateway ($GATEWAY_CIDR)..."
# manual method + never-default keeps wlan0 as the only default route
sudo nmcli con mod "$ETH_CON" \
    ipv4.method manual \
    ipv4.addresses "$GATEWAY_CIDR" \
    ipv4.never-default yes
if sudo nmcli con up "$ETH_CON"; then
    log "eth0 is up at $GATEWAY_IP"
else
    log "NOTE: could not bring up eth0 (no cable/link?) - continuing; it will activate on link"
fi

log "loading nftables rules..."
sudo systemctl enable --now nftables >/dev/null 2>&1 || true
sudo nft -f /etc/nftables.conf

log "restarting dnsmasq (DHCP+DNS on eth0)..."
sudo systemctl restart dnsmasq
sudo systemctl enable dnsmasq >/dev/null 2>&1

# ---------------------------------------------------------------- router ----
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
ROUTER_SRC="$REPO_ROOT/src/router"
if [[ -d "$ROUTER_SRC/dialback_router" ]]; then
    log "installing dialback router service..."
    sudo mkdir -p /opt/dialback /etc/dialback
    sudo rsync -a --delete "$ROUTER_SRC/" /opt/dialback/src/router/
    sudo cp "$REPO_ROOT/config/dialback.yaml" /etc/dialback/dialback.yaml
    sudo install -m 644 \
        "$OVERLAYS/etc/systemd/system/dialback-router.service" \
        /etc/systemd/system/dialback-router.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now dialback-router >/dev/null 2>&1 || true
    sudo systemctl restart dialback-router
else
    log "NOTE: $ROUTER_SRC not found - skipping router service install"
fi

log "verifying..."
systemctl is-active --quiet nftables  || { echo "nftables not active" >&2; exit 1; }
systemctl is-active --quiet dnsmasq   || { echo "dnsmasq not active" >&2; exit 1; }
if systemctl list-unit-files dialback-router.service >/dev/null 2>&1; then
    systemctl is-active --quiet dialback-router \
        || { echo "dialback-router not active" >&2; exit 1; }
fi

log "gateway bootstrap complete."
log "  upstream  : wlan0 (NAT masquerade)"
log "  downstream: $GATEWAY_CIDR on eth0 (DHCP 10.0.0.100-150, DNS served locally)"
log "  interception: tcp/80 from eth0 -> router on :8888"
