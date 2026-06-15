"""Tests for maat/serving/auth.py — user identity + session auth (P5, issue #51).

All tests use ``memory_store()`` and injectable _now so no DB is required.
Scrypt is run at its real parameters; tests are CPU-bounded (~0.5 s total) but
not flaky.  If you need a faster suite, stub ``_hash_password`` in conftest.
"""

from __future__ import annotations

import base64
import json
import pytest

from maat.serving.auth import (
    AUTH_EVENT_TYPES,
    SESSION_CREATED,
    SESSION_REVOKED,
    USER_REGISTERED,
    issue_token,
    memory_store,
    register,
    revoke,
    validate_token,
    verify_password,
)

SECRET = b"test-secret-do-not-use-in-production"

# ── helpers ─────────────────────────────────────────────────────────────────────

def _register_and_token(
    email: str = "alice@example.com",
    password: str = "correct-horse-battery",
    *,
    now: float = 1_000_000.0,
    ttl: int = 3600,
):
    """Register a user and issue a token; return (store, user_id, token, session_id)."""
    store = memory_store()
    uid = register(email, password, store)
    token = issue_token(uid, SECRET, store, ttl=ttl, _now=now)
    # Recover session_id from the store so tests can revoke it
    sessions = store.query(SESSION_CREATED)
    sid = sessions[-1]["session_id"]
    return store, uid, token, sid


# ── event constants ─────────────────────────────────────────────────────────────

def test_auth_event_types_exported():
    assert USER_REGISTERED in AUTH_EVENT_TYPES
    assert SESSION_CREATED in AUTH_EVENT_TYPES
    assert SESSION_REVOKED in AUTH_EVENT_TYPES


# ── register ────────────────────────────────────────────────────────────────────

def test_register_appends_event_with_user_id():
    store = memory_store()
    uid = register("bob@example.com", "s3cr3t", store)
    events = store.query(USER_REGISTERED)
    assert len(events) == 1
    ev = events[0]
    assert ev["user_id"] == uid
    assert ev["email"] == "bob@example.com"
    assert "salt_b64" in ev and "dk_b64" in ev


def test_register_normalises_email_to_lowercase():
    store = memory_store()
    register("  BOB@EXAMPLE.COM  ", "s3cr3t", store)
    ev = store.query(USER_REGISTERED)[0]
    assert ev["email"] == "bob@example.com"


def test_register_duplicate_email_raises():
    store = memory_store()
    register("dup@example.com", "pass1", store)
    with pytest.raises(ValueError, match="already registered"):
        register("dup@example.com", "pass2", store)


def test_register_different_users_get_different_salts():
    store = memory_store()
    register("a@x.com", "pw", store)
    register("b@x.com", "pw", store)
    evs = store.query(USER_REGISTERED)
    assert evs[0]["salt_b64"] != evs[1]["salt_b64"]


def test_register_accepts_explicit_user_id():
    store = memory_store()
    uid = register("c@x.com", "pw", store, user_id="fixed-id-123")
    assert uid == "fixed-id-123"
    assert store.query(USER_REGISTERED)[0]["user_id"] == "fixed-id-123"


# ── verify_password ─────────────────────────────────────────────────────────────

def test_correct_password_verifies():
    store = memory_store()
    uid = register("alice@example.com", "correct-horse-battery", store)
    result = verify_password("alice@example.com", "correct-horse-battery", store)
    assert result == uid


def test_wrong_password_rejected():
    store = memory_store()
    register("alice@example.com", "correct-horse-battery", store)
    result = verify_password("alice@example.com", "wrong-password", store)
    assert result is None


def test_unknown_email_rejected():
    store = memory_store()
    result = verify_password("nobody@example.com", "any-password", store)
    assert result is None


def test_verify_password_case_insensitive_email():
    store = memory_store()
    uid = register("alice@example.com", "correct-horse", store)
    result = verify_password("ALICE@EXAMPLE.COM", "correct-horse", store)
    assert result == uid


def test_password_with_unicode():
    store = memory_store()
    uid = register("unicode@example.com", "pääsalmä", store)
    assert verify_password("unicode@example.com", "pääsalmä", store) == uid
    assert verify_password("unicode@example.com", "pääsalmä_wrong", store) is None


# ── issue_token ─────────────────────────────────────────────────────────────────

def test_issue_token_returns_two_part_string():
    store, uid, token, _ = _register_and_token()
    parts = token.split(".")
    assert len(parts) == 2


def test_issue_token_appends_session_created_event():
    store, uid, token, sid = _register_and_token()
    sessions = store.query(SESSION_CREATED)
    assert len(sessions) == 1
    assert sessions[0]["user_id"] == uid
    assert sessions[0]["session_id"] == sid


def test_issue_token_unknown_user_raises():
    store = memory_store()
    with pytest.raises(ValueError, match="Unknown user"):
        issue_token("ghost-id", SECRET, store)


def test_issue_token_payload_contains_expected_fields():
    store, uid, token, sid = _register_and_token(now=1_000_000.0, ttl=3600)
    payload_b64 = token.split(".")[0]
    padding = "=" * ((-len(payload_b64)) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
    assert payload["user_id"] == uid
    assert payload["session_id"] == sid
    assert payload["expires_at"] == 1_000_000 + 3600


# ── validate_token ──────────────────────────────────────────────────────────────

def test_valid_token_returns_user_id():
    store, uid, token, _ = _register_and_token(now=1_000_000.0, ttl=3600)
    result = validate_token(token, SECRET, store, _now=1_000_000.0 + 1)
    assert result == uid


def test_expired_token_rejected():
    store, uid, token, _ = _register_and_token(now=1_000_000.0, ttl=3600)
    # travel past expiry
    result = validate_token(token, SECRET, store, _now=1_000_000.0 + 3601)
    assert result is None


def test_tampered_payload_rejected():
    store, uid, token, _ = _register_and_token(now=1_000_000.0, ttl=3600)
    payload_b64, sig = token.split(".")
    # Decode, corrupt one byte, re-encode
    padding = "=" * ((-len(payload_b64)) % 4)
    raw = bytearray(base64.urlsafe_b64decode(payload_b64 + padding))
    raw[0] ^= 0xFF
    bad_b64 = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode()
    tampered = f"{bad_b64}.{sig}"
    assert validate_token(tampered, SECRET, store, _now=1_000_000.0 + 1) is None


def test_wrong_secret_rejected():
    store, uid, token, _ = _register_and_token(now=1_000_000.0)
    other_secret = b"completely-different-secret"
    assert validate_token(token, other_secret, store, _now=1_000_000.0 + 1) is None


def test_truncated_token_rejected():
    store, uid, token, _ = _register_and_token()
    assert validate_token("notavalidtoken", SECRET, store) is None
    assert validate_token("", SECRET, store) is None
    assert validate_token("only.one.dot.too.many", SECRET, store) is None


def test_garbage_base64_payload_rejected():
    assert validate_token("!!!.%%%", SECRET, memory_store()) is None


# ── revoke ───────────────────────────────────────────────────────────────────────

def test_revoked_session_rejected():
    store, uid, token, sid = _register_and_token(now=1_000_000.0, ttl=3600)
    revoke(sid, store, user_id=uid, reason="logout")
    result = validate_token(token, SECRET, store, _now=1_000_000.0 + 1)
    assert result is None


def test_revoke_appends_session_revoked_event():
    store, uid, token, sid = _register_and_token()
    revoke(sid, store, user_id=uid, reason="test")
    evs = store.query(SESSION_REVOKED)
    assert len(evs) == 1
    assert evs[0]["session_id"] == sid
    assert evs[0]["reason"] == "test"


def test_different_session_not_affected_by_revocation():
    """Revoking session A must not invalidate a live session B for the same user."""
    store = memory_store()
    uid = register("multi@example.com", "pw", store)
    t1 = issue_token(uid, SECRET, store, session_id="sess-A", ttl=3600, _now=1_000_000.0)
    t2 = issue_token(uid, SECRET, store, session_id="sess-B", ttl=3600, _now=1_000_000.0)
    revoke("sess-A", store)
    assert validate_token(t1, SECRET, store, _now=1_000_000.0 + 1) is None
    assert validate_token(t2, SECRET, store, _now=1_000_000.0 + 1) == uid


# ── memory_store isolation ───────────────────────────────────────────────────────

def test_memory_stores_are_independent():
    s1 = memory_store()
    s2 = memory_store()
    register("a@x.com", "pw", s1)
    assert s2.query(USER_REGISTERED) == []


# ── full round-trip ───────────────────────────────────────────────────────────────

def test_full_login_flow():
    """Register → verify password → issue token → validate → revoke → reject."""
    store = memory_store()
    uid = register("user@maat.news", "correct-horse", store)

    # Wrong password
    assert verify_password("user@maat.news", "wrong", store) is None

    # Correct password
    assert verify_password("user@maat.news", "correct-horse", store) == uid

    # Issue token
    now = 2_000_000.0
    token = issue_token(uid, SECRET, store, ttl=7200, _now=now)

    # Still valid
    assert validate_token(token, SECRET, store, _now=now + 100) == uid

    # Expired
    assert validate_token(token, SECRET, store, _now=now + 7201) is None

    # Re-issue and revoke
    token2 = issue_token(uid, SECRET, store, ttl=7200, _now=now)
    sid2 = store.query(SESSION_CREATED)[-1]["session_id"]
    revoke(sid2, store)
    assert validate_token(token2, SECRET, store, _now=now + 100) is None
