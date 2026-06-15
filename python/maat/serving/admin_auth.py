"""Admin (operator-console) auth — Google OIDC + email allowlist (P8, #163; D31/D32).

**Separate from the user auth** (`serving/auth.py`, Sign in with Apple / #51): a different
identity provider (Google), a different cookie, a different code path, its own events. The
console gates on *this*; ``/api/*`` (the public app surface) is never touched here, so a
breach in the public user path can't escalate to admin.

Two layers (D31): WireGuard at the network (see ``deploy/``) and Google OIDC + a strict
email allowlist here. This module is the identity layer.

Design choices
--------------
- **Stateless session cookie.** The admin session is an HMAC-SHA256-signed cookie carrying
  ``{sub, email, iat, exp}``. Validation needs only ``MAAT_ADMIN_SESSION_SECRET`` — no DB —
  so the gate keeps working (and you can still get in over WireGuard) even if Postgres/NATS
  are down. Logout clears the cookie; with a short TTL + single operator + WG-gating that is
  the whole revocation story (a server-side revocation list would re-introduce a per-request
  DB read and is deferred).
- **Login/logout are also recorded as ``admin.session.*`` events** (best-effort) for the
  audit log — "admin identity as its own events" (D31), kept in the serving layer rather than
  kerneld-folded so an audit-write outage can never lock the operator out.
- **ID-token trust.** The ``id_token`` is read straight from Google's token endpoint over TLS
  (server-to-server, authenticated with the client secret), so per Google's OIDC guidance its
  signature need not be re-verified. We still validate ``iss``/``aud``/``exp``/``nonce``/
  ``email_verified``/``hd`` and the allowlist.

Everything here is **pure stdlib + httpx** and unit-testable without a browser, a DB, or the
network — the one I/O function (``exchange_code``) takes an injected async HTTP client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlencode

# ── Google OIDC endpoints / constants ────────────────────────────────────────────
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})
SCOPES = "openid email profile"

# ── cookies ──────────────────────────────────────────────────────────────────────
SESSION_COOKIE = "maat_admin"
STATE_COOKIE = "maat_admin_oauth"

# ── defaults ───────────────────────────────────────────────────────────────────────
DEFAULT_REDIRECT_URI = "https://admin.maat.press/admin/callback"  # D32
DEFAULT_SESSION_TTL = 12 * 60 * 60  # 12 hours
STATE_TTL = 10 * 60  # 10 minutes — just long enough for the round-trip to Google

# Paths the gate must never block (the login dance itself, plus health/favicon).
OPEN_PATHS = frozenset(
    {"/admin/login", "/admin/callback", "/admin/logout", "/healthz", "/favicon.ico"}
)


# ── configuration (read once from the box env) ──────────────────────────────────────


@dataclass(frozen=True)
class AdminConfig:
    """Resolved admin-auth configuration. ``enabled`` is False until the secrets land,
    which keeps the whole feature **inert** in dev/local/test (the gate falls open)."""

    client_id: str = ""
    client_secret: str = ""
    allowlist: frozenset[str] = field(default_factory=frozenset)
    session_secret: bytes = b""
    redirect_uri: str = DEFAULT_REDIRECT_URI
    hd: str = ""  # required Google hosted-domain (e.g. "rhbrb.com"); "" = don't check
    session_ttl: int = DEFAULT_SESSION_TTL
    cookie_secure: bool = True

    @property
    def enabled(self) -> bool:
        """True only when fully configured — gate + /admin routes activate together."""
        return bool(
            self.client_id and self.client_secret and self.session_secret and self.allowlist
        )


def parse_allowlist(raw: str) -> frozenset[str]:
    """Split ``MAAT_ADMIN_EMAILS`` (comma/space/newline separated) → lowercased set."""
    parts = raw.replace(",", " ").split()
    return frozenset(p.strip().lower() for p in parts if p.strip())


def load_config(env: Mapping[str, str]) -> AdminConfig:
    """Build an :class:`AdminConfig` from a process-environment mapping (``os.environ``)."""
    secret = env.get("MAAT_ADMIN_SESSION_SECRET", "")
    return AdminConfig(
        client_id=env.get("GOOGLE_CLIENT_ID", "").strip(),
        client_secret=env.get("GOOGLE_CLIENT_SECRET", "").strip(),
        allowlist=parse_allowlist(env.get("MAAT_ADMIN_EMAILS", "")),
        session_secret=secret.encode() if secret else b"",
        redirect_uri=env.get("MAAT_ADMIN_REDIRECT_URI", DEFAULT_REDIRECT_URI).strip(),
        hd=env.get("MAAT_ADMIN_HD", "").strip().lower(),
        session_ttl=int(env.get("MAAT_ADMIN_SESSION_TTL", DEFAULT_SESSION_TTL)),
        # https-only by default; an explicit escape hatch for http://localhost testing.
        cookie_secure=env.get("MAAT_ADMIN_COOKIE_INSECURE", "") not in ("1", "true", "yes"),
    )


# ── allowlist ────────────────────────────────────────────────────────────────────


def is_allowed(email: str, allowlist: frozenset[str]) -> bool:
    """Case-insensitive exact-match membership. Empty allowlist → nobody."""
    return bool(allowlist) and email.strip().lower() in allowlist


# ── signed-cookie primitives (HMAC-SHA256; format ``<payload_b64u>.<sig_b64u>``) ─────


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * ((-len(s)) % 4))


def sign_cookie(payload: dict[str, Any], secret: bytes) -> str:
    """Serialise + sign a small JSON payload into a tamper-evident cookie value."""
    body = _b64u(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_cookie(
    token: str, secret: bytes, *, now: float | None = None
) -> dict[str, Any] | None:
    """Return the payload iff the signature is valid and (if present) ``exp`` is in the
    future; otherwise ``None``. Constant-time signature comparison."""
    if not token or token.count(".") != 1 or not secret:
        return None
    body, sig = token.split(".")
    expected = _b64u(hmac.new(secret, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        claims = json.loads(_unb64u(body))
    except Exception:  # noqa: BLE001
        return None
    exp = claims.get("exp", 0)
    if exp and int(exp) <= int(now if now is not None else time.time()):
        return None
    return claims


# ── session + oauth-state payloads ──────────────────────────────────────────────────


def make_session(sub: str, email: str, *, ttl: int, now: float | None = None) -> dict[str, Any]:
    iat = int(now if now is not None else time.time())
    return {"sub": sub, "email": email.strip().lower(), "iat": iat, "exp": iat + ttl}


def make_state(
    next_path: str,
    *,
    state: str | None = None,
    nonce: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Transient anti-CSRF state for the Google round-trip (carried in a short-TTL cookie).
    ``state``/``nonce`` are injectable for tests; otherwise freshly random."""
    iat = int(now if now is not None else time.time())
    return {
        "state": state or secrets.token_urlsafe(24),
        "nonce": nonce or secrets.token_urlsafe(24),
        "next": next_path or "/",
        "exp": iat + STATE_TTL,
    }


# ── authorization URL ──────────────────────────────────────────────────────────────


def build_auth_url(cfg: AdminConfig, *, state: str, nonce: str) -> str:
    """Google authorization-code URL. ``prompt=select_account`` so the right Google
    account is chosen; ``hd`` (if set) nudges Google to the workspace domain."""
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "access_type": "online",
        "prompt": "select_account",
    }
    if cfg.hd:
        params["hd"] = cfg.hd
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


# ── id_token decoding + claim validation (pure; no signature re-check, see module doc) ─


def decode_id_token(id_token: str) -> dict[str, Any]:
    """Decode a JWT's claims segment (no signature verification — trusted TLS channel)."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed id_token")
    return json.loads(_unb64u(parts[1]))


def check_identity(
    claims: Mapping[str, Any],
    cfg: AdminConfig,
    *,
    nonce: str,
    now: float | None = None,
) -> tuple[str | None, str]:
    """Validate id_token claims against the config + expected ``nonce``.

    Returns ``(email, "ok")`` on success or ``(None, reason)`` on any failure — the reason
    is for server-side logging, never shown to the visitor (avoid an oracle).
    """
    now = int(now if now is not None else time.time())
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return None, "bad issuer"
    if claims.get("aud") != cfg.client_id:
        return None, "bad audience"
    if int(claims.get("exp", 0)) <= now:
        return None, "id_token expired"
    if nonce and claims.get("nonce") != nonce:
        return None, "nonce mismatch"
    if cfg.hd and (claims.get("hd") or "").lower() != cfg.hd:
        return None, "wrong hosted domain"
    email = (claims.get("email") or "").strip().lower()
    if not email or not claims.get("email_verified", False):
        return None, "email not verified"
    if not is_allowed(email, cfg.allowlist):
        return None, "not on allowlist"
    return email, "ok"


# ── token exchange (the one I/O call; client injected for testability) ───────────────


async def exchange_code(cfg: AdminConfig, code: str, *, http: Any) -> dict[str, Any]:
    """Exchange an authorization ``code`` for tokens at Google's token endpoint.

    ``http`` is an awaitable-``post`` client (an ``httpx.AsyncClient`` in production, a fake
    in tests). Returns the parsed JSON (carrying ``id_token``); raises on a non-2xx response.
    """
    resp = await http.post(
        GOOGLE_TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "redirect_uri": cfg.redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    resp.raise_for_status()
    return resp.json()
