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

## Sharing the host's web ports

The Caddy service reads optional host-owned `*.caddy` files from
`/etc/maat/caddy-host`. Leave the directory empty for the normal public listeners.
A TCP gateway can use this include to move Caddy to private listeners while
preserving the application's routes through future deployments.

The gateway must supply its observed source IP through PROXY protocol; public
listeners must never accept a caller-supplied PROXY header. Private Caddy
listeners must require the header. This preserves the WireGuard `remote_ip`
restriction on the admin site. HTTP forwarded headers do not authorize access.
Validate the complete Caddyfile before activating an override, then verify both
public denial and real WireGuard access. Protect these listeners from other
containers; loopback processes able to send PROXY headers are trusted.

`docker pull caddy:2.11.4 && python3 deploy/check-caddy-host.py` checks the real
Caddy adapter with default and gateway configurations, including private
listeners, required PROXY headers, unchanged upstreams and the WireGuard gate.
