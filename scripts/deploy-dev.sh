#!/usr/bin/env bash
#
# Dev helper: deploy the current repo state to the Pi and restart services.
# Runs on the dev machine; targets `ssh dialback`.
#
# Usage: ./scripts/deploy-dev.sh [--no-restart]

set -euo pipefail

PI="${PI_HOST:-dialback}"
NO_RESTART=false
[[ "${1:-}" == "--no-restart" ]] && NO_RESTART=true

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "[deploy] syncing code to $PI:/opt/dialback ..."
# ensure /opt/dialback exists and is owned by pi so rsync needs no sudo
ssh "$PI" 'sudo mkdir -p /opt/dialback && sudo chown -R $(whoami): /opt/dialback'
rsync -a --delete --exclude '.git' --exclude '__pycache__' \
    "$REPO_ROOT/src/"     "$PI:/opt/dialback/src/"
rsync -a --delete "$REPO_ROOT/config/"  "$PI:/opt/dialback/config/"
rsync -a --delete "$REPO_ROOT/install/" "$PI:/opt/dialback/install/"
rsync -a --delete "$REPO_ROOT/eras/"    "$PI:/opt/dialback/eras/"

echo "[deploy] installing config + service + firewall rules ..."
ssh "$PI" 'set -e
sudo mkdir -p /etc/dialback
sudo cp /opt/dialback/config/dialback.yaml /etc/dialback/dialback.yaml
sudo cp /opt/dialback/install/rootfs-overlays/etc/systemd/system/dialback-router.service \
        /etc/systemd/system/dialback-router.service
sudo cp /opt/dialback/install/rootfs-overlays/etc/nftables.conf /etc/nftables.conf
sudo cp /opt/dialback/install/rootfs-overlays/etc/dnsmasq.d/dialback.conf /etc/dnsmasq.d/
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp /opt/dialback/install/rootfs-overlays/etc/systemd/journald.conf.d/10-dialback.conf \
        /etc/systemd/journald.conf.d/
sudo systemctl daemon-reload
sudo nft -f /etc/nftables.conf
sudo systemctl restart dnsmasq
sudo systemctl try-restart systemd-journald 2>/dev/null || true'

if [[ "$NO_RESTART" == "true" ]]; then
    echo "[deploy] skipping service restart"
    exit 0
fi

echo "[deploy] restarting router service ..."
ssh "$PI" 'sudo systemctl restart dialback-router && sleep 1 && systemctl is-active dialback-router'
echo "[deploy] done."
