#!/usr/bin/env bash
# Provision / refresh the Maat prod stack on a Hetzner box: Postgres+pgvector + NATS + reader,
# fronted by Caddy (auto-TLS) exposing ONLY /api/* publicly. Idempotent — safe to re-run to apply
# edge/compose changes to an existing box.
#
# Usage: deploy/provision.sh <server-ip> [public-host] [ssh-user]
#   public-host defaults to <server-ip>.sslip.io  (free IP-based hostname with a real Let's Encrypt
#   cert; swap for a real domain whose A-record points at the box when you have one).
set -euo pipefail

IP="${1:?usage: deploy/provision.sh <server-ip> [public-host] [ssh-user]}"
HOST="${2:-$IP.sslip.io}"
RUSER="${3:-root}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=60)
SSH=(ssh "${OPTS[@]}" "$RUSER@$IP")

echo ">> [1/6] waiting for cloud-init / docker…"
"${SSH[@]}" 'cloud-init status --wait >/dev/null 2>&1 || true; docker --version'

echo ">> [2/6] copying compose + Caddyfile…"
scp "${OPTS[@]}" "$HERE/docker-compose.prod.yml" "$RUSER@$IP:/root/docker-compose.yml"
scp "${OPTS[@]}" "$HERE/Caddyfile" "$RUSER@$IP:/root/Caddyfile"

echo ">> [3/6] setting MAAT_HOST=$HOST (idempotent; preserves other .env vars)…"
"${SSH[@]}" "touch /root/.env && sed -i '/^MAAT_HOST=/d' /root/.env && echo 'MAAT_HOST=$HOST' >> /root/.env"

echo ">> [4/6] firewall: allow ssh/http/https only…"
"${SSH[@]}" 'command -v ufw >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq ufw; }; \
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; \
  ufw --force enable >/dev/null; ufw status | head -6'

echo ">> [5/6] bringing up the stack…"
"${SSH[@]}" 'cd /root && docker compose up -d --wait && docker compose ps'

echo ">> [6/6] verifying HTTPS edge (Caddy may take a few seconds to issue the cert)…"
sleep 8
curl -fsS -m 30 "https://$HOST/api/feed" -o /dev/null \
  -w "  https://$HOST/api/feed -> %{http_code} (TLS ok)\n" \
  || echo "  (cert may still be issuing — retry https://$HOST/api/feed shortly)"
echo "  operator console (private): ssh -L 8000:localhost:8000 $RUSER@$IP  →  http://localhost:8000"
echo ">> deploy OK — public read API at https://$HOST/api/"
