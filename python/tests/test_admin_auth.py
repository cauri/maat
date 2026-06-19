"""Admin-console auth (P8, #163; D31/D32) — Google OIDC + email allowlist, separate from the
user auth (serving/auth.py). Pure crypto/claim tests, plus gate + route tests that need neither
a database nor the network (the token exchange is monkeypatched; pool-touching routes avoided)."""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from maat.serving import admin_auth as aa

SECRET = b"k" * 32
ALLOW = aa.parse_allowlist("Cauri@RHBRB.com")


def _cfg(**over):
    base = dict(
        client_id="cid.apps.googleusercontent.com",
        client_secret="sekret",
        allowlist=ALLOW,
        session_secret=SECRET,
        redirect_uri="https://admin.maat.press/admin/callback",
        hd="rhbrb.com",
        cookie_secure=False,  # so the TestClient (http) sends the cookie back
    )
    base.update(over)
    return aa.AdminConfig(**base)


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _id_token(claims: dict) -> str:
    return f"{_b64u(b'{}')}.{_b64u(json.dumps(claims).encode())}.{_b64u(b'sig')}"


def _good_claims(cfg, nonce, *, now=None) -> dict:
    now = int(now if now is not None else time.time())
    return {
        "iss": "https://accounts.google.com",
        "aud": cfg.client_id,
        "exp": now + 3600,
        "nonce": nonce,
        "hd": "rhbrb.com",
        "email": "cauri@rhbrb.com",
        "email_verified": True,
        "sub": "108client",
    }


# ── allowlist ──────────────────────────────────────────────────────────────────────


def test_parse_allowlist_splits_and_lowercases():
    assert aa.parse_allowlist("A@b.com, c@D.com  e@f.com\n") == {
        "a@b.com",
        "c@d.com",
        "e@f.com",
    }
    assert aa.parse_allowlist("") == frozenset()


def test_is_allowed_case_insensitive_and_empty_denies():
    assert aa.is_allowed("CAURI@rhbrb.com", ALLOW)
    assert aa.is_allowed("  cauri@rhbrb.com ", ALLOW)
    assert not aa.is_allowed("intruder@rhbrb.com", ALLOW)
    assert not aa.is_allowed("cauri@rhbrb.com", frozenset())  # empty allowlist = nobody


# ── signed cookies ──────────────────────────────────────────────────────────────────


def test_cookie_roundtrip_and_tamper_and_expiry():
    now = 1_000_000
    payload = {"sub": "x", "email": "cauri@rhbrb.com", "exp": now + 100}
    tok = aa.sign_cookie(payload, SECRET)
    assert aa.verify_cookie(tok, SECRET, now=now) == payload
    # tamper the body → reject
    body, sig = tok.split(".")
    tampered = _b64u(b'{"sub":"evil","exp":9999999999}') + "." + sig
    assert aa.verify_cookie(tampered, SECRET, now=now) is None
    # wrong secret → reject
    assert aa.verify_cookie(tok, b"other-secret", now=now) is None
    # expired → reject
    assert aa.verify_cookie(tok, SECRET, now=now + 101) is None
    # malformed → reject (no exception)
    assert aa.verify_cookie("not-a-cookie", SECRET, now=now) is None
    assert aa.verify_cookie("", SECRET, now=now) is None


def test_cookie_without_exp_is_accepted():
    tok = aa.sign_cookie({"sub": "x"}, SECRET)
    assert aa.verify_cookie(tok, SECRET) == {"sub": "x"}


# ── session + state payloads ────────────────────────────────────────────────────────


def test_make_session_sets_window():
    s = aa.make_session("sub1", "Cauri@RHBRB.com", ttl=600, now=1000)
    assert s == {"sub": "sub1", "email": "cauri@rhbrb.com", "iat": 1000, "exp": 1600}


def test_make_state_is_random_but_injectable():
    a = aa.make_state("/runs")
    b = aa.make_state("/runs")
    assert a["state"] != b["state"] and a["nonce"] != b["nonce"]  # fresh randomness
    assert a["next"] == "/runs"
    fixed = aa.make_state("/x", state="S", nonce="N", now=0)
    assert fixed == {"state": "S", "nonce": "N", "next": "/x", "exp": aa.STATE_TTL}


# ── authorization URL ──────────────────────────────────────────────────────────────


def test_build_auth_url_carries_params_and_hd():
    url = aa.build_auth_url(_cfg(), state="ST", nonce="NO")
    assert url.startswith(aa.GOOGLE_AUTH_ENDPOINT + "?")
    assert "client_id=cid.apps.googleusercontent.com" in url
    assert "redirect_uri=https%3A%2F%2Fadmin.maat.press%2Fadmin%2Fcallback" in url
    assert "state=ST" in url and "nonce=NO" in url and "hd=rhbrb.com" in url
    assert "scope=openid+email+profile" in url
    # no hd configured → no hd param
    assert "hd=" not in aa.build_auth_url(_cfg(hd=""), state="S", nonce="N")


# ── id_token decode + claim checks ──────────────────────────────────────────────────


def test_decode_id_token_roundtrip_and_malformed():
    claims = {"email": "x@y.z", "sub": "1"}
    assert aa.decode_id_token(_id_token(claims)) == claims
    with pytest.raises(ValueError):
        aa.decode_id_token("only.two")


def test_check_identity_accepts_allowlisted_workspace_user():
    cfg = _cfg()
    email, reason = aa.check_identity(_good_claims(cfg, "NON"), cfg, nonce="NON")
    assert (email, reason) == ("cauri@rhbrb.com", "ok")


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda c: c.update(iss="https://evil.example"), "bad issuer"),
        (lambda c: c.update(aud="someone-else"), "bad audience"),
        (lambda c: c.update(exp=1), "id_token expired"),
        (lambda c: c.update(nonce="WRONG"), "nonce mismatch"),
        (lambda c: c.update(hd="other.com"), "wrong hosted domain"),
        (lambda c: c.update(email_verified=False), "email not verified"),
        (lambda c: c.update(email="intruder@rhbrb.com"), "not on allowlist"),
    ],
)
def test_check_identity_rejects(mutate, expected):
    cfg = _cfg()
    claims = _good_claims(cfg, "NON")
    mutate(claims)
    email, reason = aa.check_identity(claims, cfg, nonce="NON")
    assert email is None and reason == expected


def test_check_identity_nonce_is_mandatory_when_expected_is_empty():
    # #282: a falsy expected nonce must NOT skip the check (it used to `if nonce and ...` fall through).
    cfg = _cfg()
    email, reason = aa.check_identity(_good_claims(cfg, "real-nonce"), cfg, nonce="")
    assert email is None and reason == "nonce mismatch"


def test_check_identity_rejects_token_missing_the_nonce_claim():
    # #282: a token with no nonce claim is a mismatch, not a skip.
    cfg = _cfg()
    claims = _good_claims(cfg, "NON")
    del claims["nonce"]
    email, reason = aa.check_identity(claims, cfg, nonce="NON")
    assert email is None and reason == "nonce mismatch"


def test_fail_closed_in_prod_raises_when_console_unauthenticated():
    # #282: MAAT_ENV=prod + admin auth disabled (no secrets) → refuse to boot.
    with pytest.raises(RuntimeError, match="refusing to start"):
        aa.fail_closed_in_prod(False, {"MAAT_ENV": "prod"})


def test_fail_closed_in_prod_is_noop_when_enabled_or_not_prod():
    aa.fail_closed_in_prod(True, {"MAAT_ENV": "prod"})  # secrets present → fine
    aa.fail_closed_in_prod(False, {"MAAT_ENV": "dev"})  # not prod → dev/test falls open as before
    aa.fail_closed_in_prod(False, {})                   # MAAT_ENV unset → dev/test behaviour


# ── config loading / enabled toggle ─────────────────────────────────────────────────


def test_load_config_enabled_only_when_complete():
    assert not aa.load_config({}).enabled
    full = {
        "GOOGLE_CLIENT_ID": "cid",
        "GOOGLE_CLIENT_SECRET": "sek",
        "MAAT_ADMIN_EMAILS": "cauri@rhbrb.com",
        "MAAT_ADMIN_SESSION_SECRET": "deadbeef",
    }
    cfg = aa.load_config(full)
    assert cfg.enabled and cfg.cookie_secure  # secure by default
    assert cfg.redirect_uri == aa.DEFAULT_REDIRECT_URI
    # missing any one secret → disabled
    for k in full:
        partial = {kk: vv for kk, vv in full.items() if kk != k}
        assert not aa.load_config(partial).enabled
    # the http-localhost escape hatch
    assert not aa.load_config({**full, "MAAT_ADMIN_COOKIE_INSECURE": "1"}).cookie_secure


# ── token exchange (I/O isolated behind an injected client) ──────────────────────────


def test_exchange_code_posts_and_returns_json():
    captured = {}

    class _Resp:
        def raise_for_status(self):
            captured["raised"] = True

        def json(self):
            return {"id_token": "abc", "access_token": "t"}

    class _Http:
        async def post(self, url, data=None):
            captured["url"] = url
            captured["data"] = data
            return _Resp()

    cfg = _cfg()
    out = asyncio.run(aa.exchange_code(cfg, "the-code", http=_Http()))
    assert out["id_token"] == "abc"
    assert captured["url"] == aa.GOOGLE_TOKEN_ENDPOINT
    assert captured["data"]["code"] == "the-code"
    assert captured["data"]["grant_type"] == "authorization_code"
    assert captured["data"]["client_secret"] == "sekret"
    assert captured["raised"] is True


# ── gate + routes (TestClient; no lifespan → no DB; pool-touching routes avoided) ────


@pytest.fixture()
def client(monkeypatch):
    from starlette.testclient import TestClient

    import maat.web.app as appmod

    monkeypatch.setattr(appmod, "_ADMIN", _cfg())
    # TestClient WITHOUT a context manager does not run lifespan → no asyncpg pool created.
    return TestClient(appmod.app, raise_server_exceptions=True), appmod


def test_gate_redirects_unauthenticated_console_to_login(client):
    c, _ = client
    r = c.get("/nope", follow_redirects=False)  # unknown gated path
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login?next=/nope"


def test_gate_leaves_api_open(client):
    c, _ = client
    r = c.get("/api/does-not-exist", follow_redirects=False)
    # gate must NOT redirect /api/* to login — it 404s through to routing
    assert r.status_code == 404


def test_login_redirects_to_google_with_state_cookie(client):
    c, _ = client
    r = c.get("/admin/login", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith(aa.GOOGLE_AUTH_ENDPOINT)
    assert aa.STATE_COOKIE in r.cookies


def test_full_login_flow_then_gate_passes(client, monkeypatch):
    c, appmod = client
    # 1) start login → state cookie lands in the jar
    c.get("/admin/login", follow_redirects=False)
    st = aa.verify_cookie(c.cookies.get(aa.STATE_COOKIE), SECRET)
    assert st is not None

    # 2) Google "returns" an allowlisted id_token (exchange monkeypatched, no network)
    async def fake_exchange(cfg, code):
        return {"id_token": _id_token(_good_claims(cfg, st["nonce"]))}

    monkeypatch.setattr(appmod, "_exchange", fake_exchange)
    r = c.get(f"/admin/callback?code=xyz&state={st['state']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert aa.SESSION_COOKIE in r.cookies

    # 3) with the session cookie, the gate now lets a request through (404 = past the gate)
    r2 = c.get("/nope", follow_redirects=False)
    assert r2.status_code == 404

    # 4) logout clears the session cookie and bounces to login
    r3 = c.get("/admin/logout", follow_redirects=False)
    assert r3.status_code == 303 and r3.headers["location"] == "/admin/login"
    assert c.cookies.get(aa.SESSION_COOKIE) in (None, "")


def test_callback_denies_non_allowlisted(client, monkeypatch):
    c, appmod = client
    c.get("/admin/login", follow_redirects=False)
    st = aa.verify_cookie(c.cookies.get(aa.STATE_COOKIE), SECRET)

    async def fake_exchange(cfg, code):
        bad = _good_claims(cfg, st["nonce"])
        bad["email"] = "intruder@rhbrb.com"
        return {"id_token": _id_token(bad)}

    monkeypatch.setattr(appmod, "_exchange", fake_exchange)
    r = c.get(f"/admin/callback?code=xyz&state={st['state']}", follow_redirects=False)
    assert r.status_code == 403
    assert aa.SESSION_COOKIE not in r.cookies


def test_callback_rejects_bad_state(client):
    c, _ = client
    c.get("/admin/login", follow_redirects=False)
    r = c.get("/admin/callback?code=xyz&state=forged", follow_redirects=False)
    assert r.status_code == 400
