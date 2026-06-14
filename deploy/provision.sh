#!/usr/bin/env bash
# Deploy the Maat infra baseline to a fresh Hetzner box and verify it's healthy.
# Usage: deploy/provision.sh <server-ip> [ssh-user]
set -euo pipefail

IP="${1:?usage: deploy/provision.sh <server-ip> [ssh-user]}"
RUSER="${2:-root}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=60)

echo ">> [1/4] waiting for cloud-init to finish (Docker install)…"
ssh "${OPTS[@]}" "$RUSER@$IP" 'cloud-init status --wait >/dev/null 2>&1; docker --version'

echo ">> [2/4] copying compose…"
scp "${OPTS[@]}" "$HERE/docker-compose.prod.yml" "$RUSER@$IP:/root/docker-compose.yml"

echo ">> [3/4] bringing up the stack (--wait for healthchecks)…"
ssh "${OPTS[@]}" "$RUSER@$IP" 'cd /root && docker compose up -d --wait && docker compose ps'

echo ">> [4/4] verifying pgvector + NATS…"
ssh "${OPTS[@]}" "$RUSER@$IP" \
  'docker exec $(cd /root && docker compose ps -q postgres) psql -U maat -d maat -tAc "CREATE EXTENSION IF NOT EXISTS vector; SELECT '\''pgvector '\'' || extversion FROM pg_extension WHERE extname='\''vector'\'';"'
ssh "${OPTS[@]}" "$RUSER@$IP" \
  'docker exec $(cd /root && docker compose ps -q nats) wget -q -O- http://localhost:8222/healthz'

echo ">> deploy OK on $IP"
