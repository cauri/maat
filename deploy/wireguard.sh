#!/usr/bin/env bash
# Stand up a WireGuard mesh on the Maat box so the operator console (admin.maat.press) is
# reachable only from your own devices (D31/D32). SEPARATE from deploy/provision.sh on purpose:
# this is a deliberate, mostly one-time network change. The server side is idempotent; each run
# with a new PEER name mints another client config. Run it AFTER provision.sh has the box up.
#
# Usage:  deploy/wireguard.sh <server-ip> [peer-name] [ssh-user]
#           peer-name defaults to "laptop"; ssh-user defaults to root.
#
# It prints a ready-to-use client config (also saved to ./wg-<peer>.conf). Bring the tunnel up,
# set the Google secrets in /root/.env (see deploy/ADMIN_AUTH.md), redeploy, then open
# https://admin.maat.press/ — you'll be sent to Google to sign in; the public internet gets a 403.
#
# NOTE: not yet exercised end-to-end on the box — verify with the checks in ADMIN_AUTH.md.
set -euo pipefail

IP="${1:?usage: deploy/wireguard.sh <server-ip> [peer-name] [ssh-user]}"
PEER="${2:-laptop}"
RUSER="${3:-root}"
WG_NET="10.8.0"          # /24; server is .1, peers start at .2
WG_PORT="51820"
ADMIN_HOST="admin.maat.press"
OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=60)
SSH=(ssh "${OPTS[@]}" "$RUSER@$IP")

echo ">> [1/5] installing WireGuard…"
"${SSH[@]}" 'command -v wg >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq wireguard; }'

echo ">> [2/5] ensuring server keypair + wg0.conf (idempotent)…"
"${SSH[@]}" bash -s <<EOF
set -euo pipefail
umask 077
mkdir -p /etc/wireguard && cd /etc/wireguard
[ -f server.key ] || wg genkey | tee server.key | wg pubkey > server.pub
if [ ! -f wg0.conf ]; then
  printf '[Interface]\nAddress = ${WG_NET}.1/24\nListenPort = ${WG_PORT}\nPrivateKey = %s\n' "\$(cat server.key)" > wg0.conf
fi
systemctl enable wg-quick@wg0 >/dev/null 2>&1 || true
systemctl start wg-quick@wg0  >/dev/null 2>&1 || true
EOF

echo ">> [3/5] minting peer '${PEER}'…"
PEER_CONF=$("${SSH[@]}" bash -s <<EOF
set -euo pipefail
umask 077
cd /etc/wireguard
N=\$(grep -c '^\[Peer\]' wg0.conf 2>/dev/null || true); N=\${N:-0}
PEER_IP="${WG_NET}.\$((N + 2))"
CKEY=\$(wg genkey); CPUB=\$(printf '%s' "\$CKEY" | wg pubkey); SPUB=\$(cat server.pub)
printf '\n[Peer]\n# %s\nPublicKey = %s\nAllowedIPs = %s/32\n' "${PEER}" "\$CPUB" "\$PEER_IP" >> wg0.conf
wg set wg0 peer "\$CPUB" allowed-ips "\$PEER_IP/32" 2>/dev/null || systemctl restart wg-quick@wg0
cat <<CLIENT
[Interface]
PrivateKey = \$CKEY
Address = \$PEER_IP/32

[Peer]
PublicKey = \$SPUB
Endpoint = ${IP}:${WG_PORT}
# Tunnel the WG subnet AND the box's public IP, so admin.maat.press (a public A-record →
# the box) is reached over the tunnel and arrives with a 10.8.0.x source — the only source
# Caddy serves the console to. Your other internet traffic is untouched (not 0.0.0.0/0).
AllowedIPs = ${WG_NET}.0/24, ${IP}/32
PersistentKeepalive = 25
CLIENT
EOF
)

echo ">> [4/5] firewall: allow WireGuard UDP ${WG_PORT}…"
"${SSH[@]}" "ufw allow ${WG_PORT}/udp >/dev/null 2>&1 || true; ufw status | grep -i '${WG_PORT}' || true"

echo ">> [5/5] recording MAAT_ADMIN_HOST / MAAT_WG_SUBNET in /root/.env…"
"${SSH[@]}" "touch /root/.env; \
  grep -q '^MAAT_ADMIN_HOST=' /root/.env || echo 'MAAT_ADMIN_HOST=${ADMIN_HOST}' >> /root/.env; \
  grep -q '^MAAT_WG_SUBNET=' /root/.env || echo 'MAAT_WG_SUBNET=${WG_NET}.0/24' >> /root/.env"

OUT="wg-${PEER}.conf"
printf '%s\n' "$PEER_CONF" > "$OUT"
echo
echo "================= WireGuard client config ('${PEER}') → ${OUT} ================="
printf '%s\n' "$PEER_CONF"
echo "============================================================================="
echo "Bring it up:  wg-quick up ./${OUT}   (or import ${OUT} into the WireGuard app)"
echo "Then set Google secrets in /root/.env (deploy/ADMIN_AUTH.md) and re-run provision.sh."
echo "Console:      https://${ADMIN_HOST}/      (public visitors get 403)"
echo "Break-glass:  ssh -L 8000:localhost:8000 ${RUSER}@${IP}  →  http://localhost:8000"
