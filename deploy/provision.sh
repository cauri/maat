#!/usr/bin/env bash
# Provision / refresh the Maat prod stack on a Hetzner box: the full live pipeline (kerneld +
# extract/classify agents + acquisition clock) + Postgres+pgvector + NATS + reader, fronted by
# Caddy (auto-TLS) exposing ONLY /api/* publicly. Idempotent — safe to re-run to ship code changes
# and apply edge/compose changes to an existing box.
#
# Usage: deploy/provision.sh <server-ip> [public-host] [ssh-user]
#   public-host defaults to <server-ip>.sslip.io  (free IP-based hostname with a real Let's Encrypt
#   cert; swap for a real domain whose A-record points at the box when you have one).
#
# Secrets (ANTHROPIC_API_KEY, MISTRAL_API_KEY, APIFY_API_KEY) must already be in /root/.env on the
# box — this script never transmits them; it only sets MAAT_HOST (preserving the rest).
set -euo pipefail

IP="${1:?usage: deploy/provision.sh <server-ip> [public-host] [ssh-user]}"
HOST="${2:-$IP.sslip.io}"
RUSER="${3:-root}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=60)
SSH=(ssh "${OPTS[@]}" "$RUSER@$IP")
SSH_E="ssh ${OPTS[*]}"

echo ">> [1/7] waiting for cloud-init / docker…"
"${SSH[@]}" 'cloud-init status --wait >/dev/null 2>&1 || true; docker --version'

echo ">> [2/7] copying compose + Caddyfile…"
scp "${OPTS[@]}" "$HERE/docker-compose.prod.yml" "$RUSER@$IP:/root/docker-compose.yml"
scp "${OPTS[@]}" "$HERE/Caddyfile" "$RUSER@$IP:/root/Caddyfile"

echo ">> [3/7] syncing source (python + rust) so the images build from this checkout…"
"${SSH[@]}" 'command -v rsync >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq rsync; }'
rsync -az --delete -e "$SSH_E" \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' --exclude '.env' \
  "$REPO/python/" "$RUSER@$IP:/root/python/"
rsync -az --delete -e "$SSH_E" \
  --exclude 'target' \
  "$REPO/rust/" "$RUSER@$IP:/root/rust/"

echo ">> [4/7] setting MAAT_HOST=$HOST (idempotent; preserves other .env vars)…"
"${SSH[@]}" "touch /root/.env && sed -i '/^MAAT_HOST=/d' /root/.env && echo 'MAAT_HOST=$HOST' >> /root/.env"

echo ">> [5/7] system prep: firewall (ssh/http/https only) + swap (headroom for the rust build)…"
"${SSH[@]}" 'command -v ufw >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq ufw; }; \
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; \
  ufw --force enable >/dev/null; ufw status | head -6'
# The maat-kerneld release build (sqlx + tokio + async-nats) can peak past this 3.7 GiB box; a
# 2 GiB swapfile keeps `docker compose up --build` from OOM-killing rustc. Idempotent.
"${SSH[@]}" 'if [ ! -f /swapfile ]; then \
    fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile && \
    grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab; \
  fi; free -h | awk "/Swap/ {print \"  swap: \" \$0}"'

echo ">> [6/7] building images + bringing up the stack (rust build can take a few minutes)…"
# --build picks up code changes; kerneld applies the embedded migrations (incl. 0008 image_url) on
# startup. --wait blocks until services are healthy / running (or one exits, surfacing a failure).
"${SSH[@]}" 'cd /root && docker compose up -d --build --wait && docker compose ps'

echo ">> [7/7] verifying HTTPS edge (Caddy may take a few seconds to issue the cert)…"
sleep 8
curl -fsS -m 30 "https://$HOST/api/v2/feed" -o /dev/null \
  -w "  https://$HOST/api/v2/feed -> %{http_code} (TLS ok)\n" \
  || echo "  (cert may still be issuing — retry https://$HOST/api/v2/feed shortly)"
echo "  operator console (private): ssh -L 8000:localhost:8000 $RUSER@$IP  →  http://localhost:8000"
echo ">> deploy OK — public read API at https://$HOST/api/"
