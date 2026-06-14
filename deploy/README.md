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
