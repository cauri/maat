# Admin console auth — setup & break-glass (P8, #163; D31/D32)

The operator console (`admin.maat.press`) is protected by **two independent layers**, separate
from the app's user auth (Sign in with Apple, #51):

1. **Network — WireGuard.** The console is *served* only to devices on your private WireGuard
   mesh. Everyone else on the internet gets a `403` (they can see the name exists, nothing more).
2. **Identity — Google OIDC + allow-list.** Even on the mesh, you must sign in with an
   allow-listed Google account (`cauri@rhbrb.com`). Admin logins are recorded as
   `admin.session.*` events for the audit log.

If **either** layer isn't configured yet, the app **fails safe**: with no `MAAT_ADMIN_*` secrets
the gate stays off in dev/local, and on the box the WireGuard layer 403s everyone until a peer
connects. Turn both on for production.

---

## One-time setup

### 1. Google OAuth client — ✅ done
Created in Google Cloud project **maat** (`maat-499519`): app **"Maat Admin"** (Internal,
Workspace `rhbrb.com`), Web client **"Maat Admin Console"**, redirect URI
`https://admin.maat.press/admin/callback`. This produced a **Client ID** and **Client Secret**.

### 2. Secrets on the box — your action (never in the repo or chat)
Add these to `/root/.env` on the box (same file as `POSTGRES_*`):

```sh
GOOGLE_CLIENT_ID=<the client id from Google>
GOOGLE_CLIENT_SECRET=<the client secret from Google>
MAAT_ADMIN_EMAILS=cauri@rhbrb.com
MAAT_ADMIN_SESSION_SECRET=$(openssl rand -hex 32)   # paste the generated value
# optional hardening — require the Google Workspace domain on the id_token:
MAAT_ADMIN_HD=rhbrb.com
```

Then re-run `deploy/provision.sh <ip> api.maat.press` to recreate the `reader` with the new env.
(With all four set, the console logs `admin auth ENABLED`; without them it logs
`admin auth DISABLED — console is UNAUTHENTICATED`.)

### 3. WireGuard mesh
The `admin.maat.press` A-record is already public → the box. Stand up the mesh and mint a peer:

```sh
deploy/wireguard.sh <ip> laptop        # repeat with a new name for each device, e.g. phone
```

It prints (and saves `wg-laptop.conf`) a client config. Import it into the WireGuard app, or:

```sh
wg-quick up ./wg-laptop.conf
```

---

## Logging in
With the tunnel up: open **https://admin.maat.press/** → you're redirected to Google → pick
`cauri@rhbrb.com` → back to the console. Sessions last 12h (`MAAT_ADMIN_SESSION_TTL`); "Sign out"
is in the nav. Public visitors (no tunnel) get a `403`; allowed-but-not-listed accounts get a
"not allowed" page.

## Break-glass (if WireGuard or Google is down)
The SSH tunnel still reaches the console directly on the box (bypasses Caddy/WG, but **not** the
app's Google gate unless you also unset the secrets):

```sh
ssh -L 8000:localhost:8000 root@<ip>     # then open http://localhost:8000
```

To fully disable admin auth in an emergency: remove the `MAAT_ADMIN_*`/`GOOGLE_*` lines from
`/root/.env` and redeploy — the console reverts to unauthenticated (only do this behind the SSH
tunnel / on the trusted network).

---

## Verify it's working
```sh
# from a NON-WG network: should be 403 (name resolves, console not served)
curl -sS -o /dev/null -w '%{http_code}\n' https://admin.maat.press/        # → 403
# the public API is unaffected:
curl -sS -o /dev/null -w '%{http_code}\n' https://api.maat.press/api/feed   # → 200
# with the tunnel UP: should be 303 → /admin/login (then Google)
curl -sS -o /dev/null -w '%{http_code}\n' https://admin.maat.press/         # → 303
```
On the box: `wg show` lists peers; `docker compose logs caddy | grep admin.maat.press` shows the
cert was issued.

## Manual WireGuard fallback
If `wireguard.sh` needs tweaking, the equivalent by hand on the box:
```sh
apt-get install -y wireguard
cd /etc/wireguard && umask 077
wg genkey | tee server.key | wg pubkey > server.pub
cat > wg0.conf <<EOF
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = $(cat server.key)
EOF
systemctl enable --now wg-quick@wg0
ufw allow 51820/udp
# add a peer: generate client keys, append a [Peer] block (AllowedIPs = 10.8.0.2/32),
# and hand the client: its key, Address 10.8.0.2/32, Endpoint <ip>:51820,
# AllowedIPs 10.8.0.0/24, <ip>/32
```

## Why this shape (D31/D32)
- **Public name + WG-gated serving** (not a hidden name + ACME DNS-01): the only thing leaked is
  "a host called admin.maat.press exists"; reachability (WG) and login (Google allow-list) are
  both gated, and we avoid putting an OVH API token on the box.
- **Caddy on the host network** so the `remote_ip` matcher sees the true WG source IP (a bridge
  + published port would masquerade it). The client tunnels the box's public IP so requests to
  `admin.maat.press` arrive as `10.8.0.x`.
- **Stateless signed session cookie** so the gate keeps working without the DB/bus (break-glass).
