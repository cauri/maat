"""User identity + session auth for the serving layer (P5, issue #51).

DRAFT — needs security review before production use.

Design
------
Users and sessions are represented as **events** on the shared append-only log,
not as schema tables.  Three event types live here:

    user.registered   — {user_id, email, salt_b64, dk_b64}
    session.created   — {session_id, user_id, expires_at}
    session.revoked   — {session_id, user_id, reason}

All crypto is stdlib-only (hashlib, hmac, secrets, base64). Password hashing uses
hashlib.scrypt with a per-user random salt; constant-time comparison via
hmac.compare_digest. Session tokens are HMAC-SHA256 signed (b64url, no padding).

Injectable store
----------------
All functions accept an explicit ``store`` argument — either a ``MemoryStore``
produced by ``memory_store()`` (for tests) or an async ``DbStore`` wrapping the
real asyncpg pool. This keeps all crypto pure and testable without a DB.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

# ── event type constants ────────────────────────────────────────────────────────

USER_REGISTERED = "user.registered"
SESSION_CREATED = "session.created"
SESSION_REVOKED = "session.revoked"

AUTH_EVENT_TYPES = frozenset({USER_REGISTERED, SESSION_CREATED, SESSION_REVOKED})

# ── scrypt parameters (NIST SP 800-132 §5.4 recommendation floor) ──────────────
# These are deliberately conservative; bump N for higher-security deployments.
_SCRYPT_N = 2**14   # CPU/mem cost; 16 384 — minimum for interactive logins
_SCRYPT_R = 8       # block size
_SCRYPT_P = 1       # parallelism
_SCRYPT_DKLEN = 32  # 256-bit derived key

# Default token TTL: 24 hours
_DEFAULT_TTL_SECONDS = 86_400


# ── event store protocol (sync in-memory for tests; async wrapper for production) ─

class EventStore(Protocol):
    """Minimal store contract: append an event, query by type."""

    def append(self, type_: str, stream_id: str, data: dict[str, Any]) -> None: ...

    def query(self, type_: str) -> list[dict[str, Any]]: ...


@dataclass
class MemoryStore:
    """Pure in-memory store — no I/O, safe to use in any test without mocking."""
    _events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def append(self, type_: str, stream_id: str, data: dict[str, Any]) -> None:
        self._events.append((type_, stream_id, data))

    def query(self, type_: str) -> list[dict[str, Any]]:
        return [data for (t, _, data) in self._events if t == type_]


def memory_store() -> MemoryStore:
    """Return a fresh in-memory store suitable for unit tests."""
    return MemoryStore()


# ── password hashing ────────────────────────────────────────────────────────────

def _hash_password(password: str, salt: bytes) -> bytes:
    """Derive a key from *password* + *salt* using scrypt.  Pure — no I/O."""
    return hashlib.scrypt(
        password.encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _unb64(s: str) -> bytes:
    return base64.b64decode(s)


# ── user registration + lookup ──────────────────────────────────────────────────

def register(
    email: str,
    password: str,
    store: EventStore,
    *,
    user_id: str | None = None,
) -> str:
    """Register a new user; return the ``user_id``.

    Generates a random salt, hashes the password with scrypt, and appends a
    ``user.registered`` event.  Raises ``ValueError`` if the email is already
    taken (based on the event log).
    """
    email = email.strip().lower()
    existing = _find_user_by_email(email, store)
    if existing is not None:
        raise ValueError(f"Email already registered: {email}")

    uid = user_id or secrets.token_hex(16)
    salt = secrets.token_bytes(32)
    dk = _hash_password(password, salt)
    store.append(
        USER_REGISTERED,
        uid,
        {
            "user_id": uid,
            "email": email,
            "salt_b64": _b64(salt),
            "dk_b64": _b64(dk),
        },
    )
    return uid


def _find_user_by_email(email: str, store: EventStore) -> dict[str, Any] | None:
    for ev in store.query(USER_REGISTERED):
        if ev.get("email") == email:
            return ev
    return None


def _find_user_by_id(user_id: str, store: EventStore) -> dict[str, Any] | None:
    for ev in store.query(USER_REGISTERED):
        if ev.get("user_id") == user_id:
            return ev
    return None


# ── password verification ───────────────────────────────────────────────────────

def verify_password(email: str, password: str, store: EventStore) -> str | None:
    """Verify *email* + *password*; return ``user_id`` on success, ``None`` on failure.

    Uses hmac.compare_digest for constant-time equality so timing side-channels
    do not leak whether the email exists or the password is wrong.
    """
    email = email.strip().lower()
    ev = _find_user_by_email(email, store)
    if ev is None:
        # Run a dummy hash so timing is indistinguishable from "wrong password"
        _hash_password(password, b"\x00" * 32)
        return None

    salt = _unb64(ev["salt_b64"])
    stored_dk = _unb64(ev["dk_b64"])
    candidate_dk = _hash_password(password, salt)
    if hmac.compare_digest(stored_dk, candidate_dk):
        return ev["user_id"]
    return None


# ── session token issuance + validation ────────────────────────────────────────

def _token_payload(session_id: str, user_id: str, expires_at: int) -> bytes:
    """Canonical bytes to sign: deterministic JSON, sorted keys."""
    return json.dumps(
        {"session_id": session_id, "user_id": user_id, "expires_at": expires_at},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sign(payload: bytes, secret: bytes) -> str:
    """Return a URL-safe base64 HMAC-SHA256 signature (no padding)."""
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def issue_token(
    user_id: str,
    secret: bytes,
    store: EventStore,
    *,
    session_id: str | None = None,
    ttl: int = _DEFAULT_TTL_SECONDS,
    _now: float | None = None,
) -> str:
    """Issue a signed session token for *user_id*; append a ``session.created`` event.

    Token format (dot-separated, all URL-safe base64-no-padding):
        <payload_b64>.<signature_b64>

    where payload is the JSON ``{session_id, user_id, expires_at}``.

    ``_now`` is injectable for deterministic tests.
    """
    if _find_user_by_id(user_id, store) is None:
        raise ValueError(f"Unknown user: {user_id}")

    now = int(_now if _now is not None else time.time())
    sid = session_id or secrets.token_hex(16)
    expires_at = now + ttl

    store.append(
        SESSION_CREATED,
        sid,
        {"session_id": sid, "user_id": user_id, "expires_at": expires_at},
    )

    payload = _token_payload(sid, user_id, expires_at)
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    sig = _sign(payload, secret)
    return f"{payload_b64}.{sig}"


def validate_token(
    token: str,
    secret: bytes,
    store: EventStore,
    *,
    _now: float | None = None,
) -> str | None:
    """Validate a token; return ``user_id`` on success, ``None`` otherwise.

    Checks (in order):
      1. Well-formed (two dot-separated parts).
      2. Signature valid (HMAC constant-time).
      3. Not expired.
      4. Session not revoked.
    """
    parts = token.split(".")
    if len(parts) != 2:
        return None

    payload_b64, sig = parts
    # Re-pad for decoding
    padding = "=" * ((-len(payload_b64)) % 4)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(payload)
    except Exception:  # noqa: BLE001
        return None

    expected_sig = _sign(payload, secret)
    if not hmac.compare_digest(sig, expected_sig):
        return None

    now = int(_now if _now is not None else time.time())
    if claims.get("expires_at", 0) <= now:
        return None

    session_id = claims.get("session_id")
    if _is_revoked(session_id, store):
        return None

    return claims.get("user_id")


# ── revocation ──────────────────────────────────────────────────────────────────

def revoke(
    session_id: str,
    store: EventStore,
    *,
    reason: str = "",
    user_id: str = "",
) -> None:
    """Revoke a session by appending a ``session.revoked`` event.

    Subsequent calls to ``validate_token`` for any token carrying this
    ``session_id`` will return ``None``.
    """
    store.append(
        SESSION_REVOKED,
        session_id,
        {"session_id": session_id, "user_id": user_id, "reason": reason},
    )


def _is_revoked(session_id: str, store: EventStore) -> bool:
    return any(
        ev.get("session_id") == session_id for ev in store.query(SESSION_REVOKED)
    )
