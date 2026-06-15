# Deploy

Validates the Hetzner deploy path: provision a box → Docker installs on first boot
(`cloud-init.yaml`) → deploy the infra baseline (`docker-compose.prod.yml`:
Postgres+pgvector + NATS JetStream) → verify healthy (`provision.sh`).

## What I need (one of):
1. **Hetzner Cloud API token** (Read & Write) — I create + provision + deploy + verify, all scripted.
2. **SSH to a box you create** — Ubuntu 24.04, your existing SSH key attached, give me the IP.

## Flow with a token (CAX11 ARM, Falkenstein / fsn1, Ubuntu 24.04)
```sh
export HCLOUD_TOKEN=…                      # kept in gitignored .env, never committed
hcloud server create --name maat-dev --type cax11 --location fsn1 \
  --image ubuntu-24.04 --ssh-key <key> --user-data-from-file deploy/cloud-init.yaml
deploy/provision.sh <server-ip>
```
No public ports are opened; services are verified on-box.

## Admin console auth (P8, #163; D31/D32)
The operator console (`admin.maat.press`) is gated by WireGuard (network) + Google OIDC + an
email allow-list (identity), separate from the app's user auth. One-time setup (Google secrets in
`/root/.env`, `deploy/wireguard.sh`), login, break-glass, and verification are in
**[`ADMIN_AUTH.md`](ADMIN_AUTH.md)**.
