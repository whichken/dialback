#!/usr/bin/env bash
#
# Simulate a retro PC client from the Pi itself using a network namespace.
# Useful when no physical machine is plugged into eth0.
#
# NOTE: run AFTER deploy-dev.sh — reapplying nftables.conf wipes the
# temporary redirect rule this script adds for the veth interface.
#
# Usage: ssh dialback 'scripts/test-netns.sh'   (or run on the Pi directly)

set -euo pipefail

sudo ip netns del retrotest 2>/dev/null || true

sudo ip netns add retrotest
sudo ip link add veth-pi type veth peer name veth-ns
sudo ip link set veth-ns netns retrotest
sudo ip addr add 10.0.9.1/24 dev veth-pi
sudo ip link set veth-pi up
sudo mkdir -p /etc/netns/retrotest
echo "nameserver 10.0.0.1" | sudo tee /etc/netns/retrotest/resolv.conf >/dev/null
sudo ip netns exec retrotest ip link set lo up
sudo ip netns exec retrotest ip addr add 10.0.9.100/24 dev veth-ns
sudo ip netns exec retrotest ip link set veth-ns up
sudo ip netns exec retrotest ip route add default via 10.0.9.1

# route the simulated client's port-80 traffic into the router, same as eth0
sudo nft add rule inet dialback prerouting iifname "veth-pi" \
    ip daddr != 10.0.0.1 tcp dport 80 redirect to :8888

echo "[test-netns] ready. Example:"
echo "  sudo ip netns exec retrotest curl -sI http://example.com"
