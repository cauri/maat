"""Maat operator console — the reader (§5.7) evolved into the admin surface (P8).

- **Content** — the corroborated-stories feed, plus per-claim and per-cluster inspectors
  (F2): full provenance, and the confidence read shown with its derivation.
- **Audit** — every operator action, read straight off the event log (F1; D5).
- **Corrections** — operator fixes published as typed admin events (F3); the kernel folds
  them and marks the row `corrected` so a pipeline re-run will not clobber the fix.

Admin actions ARE events (D5/D20): the console publishes to NATS and reads the Postgres
projections; maat-kerneld is the single writer. Behind-the-box — no auth yet (rides P5).
Run: `make web`.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import asyncpg
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from maat import config, events, prompts
from maat.agents.triage import classify as triage_classify
from maat.bus import connect as nats_connect
from maat.clocks import is_paused, read_topics
from maat.eval_prompt import eval_goldens
from maat.eval_prompt import summary as eval_prompt_summary
from maat.evals import evaluate, load_expectations
from maat.learning.calibration import Weights, replay_ab
from maat.learning.calibration_prod import production_calibration
from maat.learning.reputation import (
    fold_reputation,
    reputation_score,
    reputation_trajectories,
)
from maat.learning.rl import policy_step
from maat.learning.trajectory import load_trajectory
from maat.learning import source_registry as sreg
from maat.metrics import de_us
from maat.obs_metrics import pipeline_health
from maat.pipeline.corroborate import (
    ClaimRow,
    cluster_id,
    confidence_label as _confidence_label,
    confidence_read,
    corroborate_fixed,
    is_primary_source,
)
from maat.providers import seam
from maat.serving import admin_auth
from maat.serving import spend as spend_mod
from maat.serving.feed import feed_router
from maat.serving.social_api import social_router
from maat.serving.translate import translate_text
from maat.serving.feedback import queue as feedback_queue
from maat.serving.feedback import record as feedback_record
from maat.serving.feedback import record_triage as feedback_record_triage
from maat.serving.feedback import routed_queue

CATCAFE_URL = os.environ.get("CATCAFE_URL", "http://localhost:8800")
ROOT = Path(__file__).resolve().parents[3]  # repo root (for config/topics.txt)
_BUS_DOWN = "Couldn't reach the event bus — nothing was saved."

DB = os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")

# Local admin event type (#123) — kept here, never edited into events.py (the kernel folds it
# the same way it folds admin.threshold.changed; for the console it is purely an audit marker).
ADMIN_THRESHOLD_REVERTED = "admin.threshold.reverted"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DB)
    try:
        app.state.nats = await nats_connect()
    except Exception as exc:  # noqa: BLE001 - the reader must still serve if NATS is down
        app.state.nats = None
        print(f"[console] NATS unavailable, corrections disabled: {exc}", flush=True)
    yield
    await app.state.pool.close()
    if app.state.nats is not None:
        await app.state.nats.close()


app = FastAPI(lifespan=lifespan, title="Maat operator console")

# Central stylesheet (and any future assets) served from maat/web/static/. The Dockerfile's
# `COPY maat ./maat` ships it; the admin gate leaves /static open.
app.mount("/static", StaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")

# Mount the served-feed APIRouter (serving/feed.py) so the Apple client reads /api/v2/feed and
# /api/v2/story/{id} — confidence labels + de-US re-ranking — off the same projections this
# console reads. The router is None only if FastAPI is unavailable at import (test env guard).
if feed_router is not None:
    app.include_router(feed_router)

# Comments + pins (#49) — client-facing /api/v2 routes over the event-sourced social layer
# (serving/social.py), folded read-time like the admin events. None only if FastAPI is absent.
if social_router is not None:
    app.include_router(social_router)


# ============================ admin auth (P8, #163; D31/D32) ============================
# Google OIDC + a strict email allowlist, SEPARATE from the user auth (serving/auth.py / Sign in
# with Apple). Inert until the box env carries GOOGLE_CLIENT_ID/_SECRET + MAAT_ADMIN_EMAILS +
# MAAT_ADMIN_SESSION_SECRET — until then `.enabled` is False, the gate falls open, and /admin/*
# just redirects home, so dev/local/test behave exactly as before. Layered behind WireGuard (D31).
_ADMIN = admin_auth.load_config(os.environ)
if not _ADMIN.enabled:
    print(
        "[console] admin auth DISABLED (no MAAT_ADMIN_* secrets) — console is UNAUTHENTICATED",
        flush=True,
    )


@app.middleware("http")
async def _admin_gate(request: Request, call_next):
    """Require a valid admin session for every console page. ``/api/*`` (the public app surface)
    and the login dance itself stay open; everything else 303→/admin/login when not signed in."""
    if not _ADMIN.enabled:
        return await call_next(request)
    path = request.url.path
    if path.startswith("/api") or path in admin_auth.OPEN_PATHS or path.startswith("/static"):
        return await call_next(request)
    claims = admin_auth.verify_cookie(
        request.cookies.get(admin_auth.SESSION_COOKIE, ""), _ADMIN.session_secret
    )
    if claims is None:
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/admin/login?next={quote(nxt)}", status_code=303)
    request.state.admin = claims
    return await call_next(request)


def _set_cookie(resp, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(
        name, value, max_age=max_age, httponly=True,
        secure=_ADMIN.cookie_secure, samesite="lax", path="/",
    )


async def _exchange(cfg: admin_auth.AdminConfig, code: str) -> dict:
    """Google code→token exchange with our own short-lived httpx client. Monkeypatched in tests."""
    import httpx

    async with httpx.AsyncClient(timeout=15) as http:
        return await admin_auth.exchange_code(cfg, code, http=http)


async def _audit_session(event_type: str, payload: dict) -> None:
    """Best-effort publish of admin.session.* for the audit log — never blocks/gates a login."""
    nc = getattr(app.state, "nats", None)
    if nc is None:
        return
    sub = payload.get("sub", "") or "admin"
    try:
        await events.publish(
            nc, event_type, sub, events.admin_event(sub, actor=payload.get("email", ""))
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[console] admin audit publish failed ({event_type}): {exc}", flush=True)


@app.get("/admin/login")
async def admin_login(next: str = "/"):
    if not _ADMIN.enabled:
        return RedirectResponse("/", status_code=303)
    st = admin_auth.make_state(next if next.startswith("/") else "/")
    resp = RedirectResponse(
        admin_auth.build_auth_url(_ADMIN, state=st["state"], nonce=st["nonce"]), status_code=303
    )
    _set_cookie(
        resp, admin_auth.STATE_COOKIE,
        admin_auth.sign_cookie(st, _ADMIN.session_secret), admin_auth.STATE_TTL,
    )
    return resp


@app.get("/admin/callback")
async def admin_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if not _ADMIN.enabled:
        return RedirectResponse("/", status_code=303)
    if error or not code or not state:
        return HTMLResponse(_login_failed("Sign-in was cancelled or didn't complete."), 400)
    st = admin_auth.verify_cookie(
        request.cookies.get(admin_auth.STATE_COOKIE, ""), _ADMIN.session_secret
    )
    if not st or not hmac.compare_digest(str(st.get("state", "")), state):
        return HTMLResponse(_login_failed("Your sign-in link expired — please try again."), 400)
    try:
        tok = await _exchange(_ADMIN, code)
        claims = admin_auth.decode_id_token(tok["id_token"])
    except Exception as exc:  # noqa: BLE001
        print(f"[console] admin token exchange failed: {exc}", flush=True)
        return HTMLResponse(_login_failed("Couldn't complete sign-in with Google."), 502)
    email, reason = admin_auth.check_identity(claims, _ADMIN, nonce=str(st.get("nonce", "")))
    if email is None:
        print(f"[console] admin login denied ({reason}): {claims.get('email')!r}", flush=True)
        return HTMLResponse(_login_failed("That account isn't allowed to use this console."), 403)
    sess = admin_auth.make_session(str(claims.get("sub", email)), email, ttl=_ADMIN.session_ttl)
    await _audit_session(events.ADMIN_SESSION_CREATED, sess)
    nxt = st.get("next", "/")
    nxt = nxt if isinstance(nxt, str) and nxt.startswith("/") else "/"
    resp = RedirectResponse(nxt, status_code=303)
    _set_cookie(
        resp, admin_auth.SESSION_COOKIE,
        admin_auth.sign_cookie(sess, _ADMIN.session_secret), _ADMIN.session_ttl,
    )
    _set_cookie(resp, admin_auth.STATE_COOKIE, "", 0)  # clear the one-shot state cookie
    return resp


@app.get("/admin/logout")
async def admin_logout(request: Request):
    if not _ADMIN.enabled:
        return RedirectResponse("/", status_code=303)
    claims = admin_auth.verify_cookie(
        request.cookies.get(admin_auth.SESSION_COOKIE, ""), _ADMIN.session_secret
    )
    if claims:
        await _audit_session(events.ADMIN_SESSION_REVOKED, claims)
    resp = RedirectResponse("/admin/login", status_code=303)
    _set_cookie(resp, admin_auth.SESSION_COOKIE, "", 0)
    return resp


def _login_failed(msg: str) -> str:
    """A minimal standalone page (no console chrome) for a denied/failed sign-in."""
    return (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" '
        'content="width=device-width,initial-scale=1"><title>Maat — sign in</title>'
        '<div style="max-width:30rem;margin:18vh auto;padding:0 1.5rem;'
        'font:16px/1.6 -apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;color:#1c1b19">'
        '<h1 style="font-size:20px;letter-spacing:-.02em">Maat operator console</h1>'
        f'<p style="color:#7a7770">{html.escape(msg)}</p>'
        '<p><a href="/admin/login" style="color:#175fa5;font-weight:600">Try signing in again →</a></p>'
        "</div>"
    )


# ============================ routes: content (feed + inspectors) ============================


@app.get("/", response_class=HTMLResponse)
async def feed(ok: str = "") -> str:
    pool = app.state.pool
    articles = await pool.fetch(
        "select id, title, source, language, url from articles order by ingested_at desc"
    )
    claims = await pool.fetch(
        "select id, article_id, voice, speaker, kind, is_synthesis, horizon, in_headline, text "
        "from claims order by created_at"
    )
    clusters = await pool.fetch(
        "select id, fact, sources, originators, independent_originators, has_primary, "
        "confidence, extremity from clusters order by confidence desc, independent_originators desc"
    )
    id_to_source = {a["id"]: a["source"] for a in articles}
    by_article: dict[str, list] = {}
    for c in claims:
        by_article.setdefault(c["article_id"], []).append(c)
    # English gloss for non-English titles (#54), cached by the translate-titles step (latest wins).
    title_en: dict[str, str] = {}
    for r in await pool.fetch("select data from events where type = 'article.title_en' order by id"):
        d = _jobj(r["data"])
        if d.get("article_id") and d.get("title_en"):
            title_en[d["article_id"]] = d["title_en"]
    return _feed_page(articles, by_article, clusters, id_to_source, title_en=title_en, flash=ok)


@app.get("/cluster/{cid}", response_class=HTMLResponse)
async def cluster_detail(cid: str, ok: str = "") -> str:
    pool = app.state.pool
    cl = await pool.fetchrow("select * from clusters where id = $1", cid)
    if cl is None:
        return _doc('<div class="ins">'
                    '<p class="empty">No such cluster.</p></div>', "cluster", "content")
    member_ids = _jload(cl["claim_ids"])
    members = await pool.fetch(
        "select c.*, a.source as art_source from claims c join articles a on a.id = c.article_id "
        "where c.id = any($1::uuid[]) order by c.created_at",
        member_ids,
    )
    arts = await pool.fetch("select id, source from articles")
    id_to_source = {a["id"]: a["source"] for a in arts}
    others = await pool.fetch(
        "select id, fact from clusters where id <> $1 order by created_at desc", cid
    )
    return _doc(_cluster_page(cl, members, id_to_source, others), "cluster", "content", flash=ok)


@app.get("/claim/{clid}", response_class=HTMLResponse)
async def claim_detail(clid: str, ok: str = "") -> str:
    pool = app.state.pool
    c = await pool.fetchrow(
        "select c.*, a.source as art_source, a.title as art_title, a.url as art_url, "
        "a.language as art_language from claims c join articles a on a.id = c.article_id "
        "where c.id = $1",
        clid,
    )
    if c is None:
        return _doc('<div class="ins">'
                    '<p class="empty">No such claim.</p></div>', "claim", "content")
    prov = await pool.fetch(
        "select type, created_at from events where stream_id = $1 or data->>'target' = $2 "
        "order by id",
        c["article_id"],
        clid,
    )
    return _doc(_claim_page(c, prov), "claim", "content", flash=ok)


@app.get("/audit", response_class=HTMLResponse)
async def audit(limit: int = 200, ok: str = "") -> str:
    rows = await app.state.pool.fetch(
        "select type, data, created_at from events where type like 'admin.%' "
        "order by id desc limit $1",
        limit,
    )
    return _doc(_audit_page(rows), "audit", "audit", flash=ok)


@app.get("/acquisition", response_class=HTMLResponse)
async def acquisition(ok: str = "") -> str:
    """The maat.press acquisition funnel (event-sourced): page views, store clicks (which show
    'coming soon'), and the launch list — folded from acquisition.* events by the kernel."""
    pool = app.state.pool
    try:
        counts = {
            r["kind"]: r["n"]
            for r in await pool.fetch("select kind, count(*) n from acquisition_signals group by kind")
        }
        funnel = {
            "views": counts.get("view", 0),
            "clicks": counts.get("click", 0),
            "notifies": counts.get("notify", 0),
            "signups": await pool.fetchval("select count(*) from acquisition_signups"),
            "beta": await pool.fetchval("select count(*) from acquisition_signups where beta"),
        }
        by_platform = [
            dict(r)
            for r in await pool.fetch(
                "select platform, count(*) clicks from acquisition_signals "
                "where kind = 'click' group by platform order by clicks desc"
            )
        ]
        referrers = [
            dict(r)
            for r in await pool.fetch(
                "select coalesce(nullif(referrer, ''), 'direct') referrer, count(*) clicks "
                "from acquisition_signals where kind = 'click' group by 1 order by clicks desc limit 10"
            )
        ]
        daily = [
            dict(r)
            for r in await pool.fetch(
                "select date_trunc('day', created_at)::date as \"day\", "
                "count(*) filter (where kind = 'view') views, "
                "count(*) filter (where kind = 'click') clicks "
                "from acquisition_signals where created_at > now() - interval '14 days' "
                "group by 1 order by 1"
            )
        ]
        signups = [
            dict(r)
            for r in await pool.fetch(
                "select email, platform, beta, first_seen, hits from acquisition_signups "
                "order by first_seen desc limit 500"
            )
        ]
        ready = True
    except asyncpg.UndefinedTableError:  # migration 0009 not applied yet — degrade, don't 500
        funnel = {"views": 0, "clicks": 0, "notifies": 0, "signups": 0, "beta": 0}
        by_platform, referrers, daily, signups, ready = [], [], [], [], False
    return _doc(
        _acquisition_page(funnel, by_platform, referrers, daily, signups, ready),
        "acquisition", "acquisition", flash=ok,
    )


@app.get("/acquisition/signups.csv")
async def acquisition_signups_csv():
    """Export the launch list (the only PII this funnel holds) as CSV for the operator."""
    pool = app.state.pool
    try:
        rows = await pool.fetch(
            "select email, platform, beta, first_seen, hits from acquisition_signups "
            "order by first_seen"
        )
    except asyncpg.UndefinedTableError:
        rows = []
    out = ["email,platform,beta,first_seen,hits"]
    for r in rows:
        email = (r["email"] or "").replace('"', '""')
        out.append(
            f'"{email}",{r["platform"] or ""},{str(bool(r["beta"])).lower()},'
            f'{r["first_seen"]:%Y-%m-%dT%H:%M:%S},{r["hits"]}'
        )
    return Response(
        "\n".join(out) + "\n",
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=maat-launch-list.csv"},
    )


@app.get("/eval", response_class=HTMLResponse)
async def eval_view(ok: str = "") -> str:
    """A4a — surface the eval harness (#32) over the live projections. Includes, never rebuilds:
    the same `evaluate()` the CLI runs, rendered for the operator. Golden regression + metrics."""
    pool = app.state.pool
    clusters = [
        dict(r)
        for r in await pool.fetch(
            "select fact, sources, originators, independent_originators, has_primary, "
            "confidence, extremity from clusters"
        )
    ]
    claims = [dict(r) for r in await pool.fetch("select kind from claims")]
    try:
        report = evaluate(clusters, claims, load_expectations())
        err = ""
    except FileNotFoundError as exc:  # no fixtures checked out
        report, err = None, f"eval fixtures not found: {exc}"
    otlp = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    return _doc(_eval_page(report, err, otlp), "eval", "eval", flash=ok)


@app.get("/spend", response_class=HTMLResponse)
async def spend_view(ok: str = "") -> str:
    """What Maat has spent so far. LLM is ESTIMATED from per-stage call counts in the event log
    (cumulative across ticks) × per-model token estimates — cat-cafe has the exact per-call truth;
    Apify is the ACTUAL figure from its billing API. Display-only; never a veracity signal."""
    pool = app.state.pool
    counts = {
        r["type"]: r["n"]
        for r in await pool.fetch(
            "select type, count(*) n from events where type in "
            "('claims.extracted','claims.classified','cluster.corroborated') group by type"
        )
    }
    n_claims = await pool.fetchval("select count(*) from claims") or 0
    rows, llm_total = spend_mod.estimate_llm_spend(
        extract_calls=counts.get("claims.extracted", 0),
        classify_calls=counts.get("claims.classified", 0),
        extremity_calls=counts.get("cluster.corroborated", 0),
        embed_claims=n_claims,
    )
    # #241: per-acquisition-channel cost — what each sourcing method (rss / apify / per-locale /
    # cc-news / backfill) costs to run through the pipeline. Articles per provider × per-article est.
    n_articles = await pool.fetchval("select count(*) from articles") or 0
    by_provider = {
        (r["p"] or "untagged"): r["n"]
        for r in await pool.fetch(
            "select coalesce(nullif(data->>'provider',''),'untagged') p, count(*) n "
            "from events where type = 'article.ingested' group by 1"
        )
    }
    provider_spend = spend_mod.spend_by_provider(
        by_provider, avg_claims_per_article=(n_claims / n_articles if n_articles else 0.0)
    )
    apify_usd = await asyncio.to_thread(spend_mod.apify_spend_usd)  # blocking httpx → off-loop
    otlp = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    budget = spend_mod.cap_status(await _today_spend_usd(pool), spend_mod.daily_cap_usd())
    return _doc(
        _spend_page(rows, llm_total, apify_usd, otlp, budget, provider_spend),
        "spend", "spend", flash=ok,
    )


@app.get("/runs", response_class=HTMLResponse)
async def runs(ok: str = "") -> str:
    """F4 — run console: pipeline activity off the event log, dead-letters, run-intent."""
    pool = app.state.pool
    agg = await pool.fetch("select type, count(*) n, max(created_at) last from events group by type")
    counts = {r["type"]: {"n": r["n"], "last": r["last"]} for r in agg}
    proj = {
        "articles": await pool.fetchval("select count(*) from articles"),
        "claims": await pool.fetchval("select count(*) from claims"),
        "clusters": await pool.fetchval("select count(*) from clusters"),
        "events": await pool.fetchval("select count(*) from events"),
    }
    recent = await pool.fetch("select type, stream_id, created_at from events order by id desc limit 25")
    try:
        dead = await pool.fetch(
            "select type, stream_id, error, created_at from dead_letters order by id desc limit 25"
        )
        dead_ready = True
    except asyncpg.UndefinedTableError:  # migration not applied yet — degrade, don't 500
        dead, dead_ready = [], False
    return _doc(
        _runs_page(stage_summary(counts), proj, recent, dead, dead_ready), "runs", "runs", flash=ok
    )


# The old per-stage "Log a run" route was removed — the console now runs the WHOLE pipeline on one
# click (acquire → extract+classify → corroborate) via /runs/run-all, with live progress on /runs/status.


@app.get("/clocks", response_class=HTMLResponse)
async def clocks_view(ok: str = "") -> str:
    """A1 — inspect + pause/resume the two clocks (§9). Ingestion is live; harvester (#39) pending."""
    pool = app.state.pool
    ing = await pool.fetchrow(
        "select count(*) n, max(created_at) last from events where type = 'article.ingested'"
    )
    daily = await pool.fetch(
        "select date_trunc('day', created_at) d, count(*) n from events "
        "where type = 'article.ingested' group by d order by d desc limit 7"
    )
    clk = await pool.fetch(
        "select data from events where type = $1 order by id desc limit 20", events.ADMIN_CLOCK_SET
    )
    paused = is_paused([_jobj(r["data"]) for r in clk], "ingestion")
    return _doc(_clocks_page(ing, daily, read_topics(ROOT), paused), "clocks", "clocks", flash=ok)


@app.post("/clocks/set")
async def clock_set(clock: str = Form(...), paused: str = Form(...), reason: str = Form("")):
    # Ops control (acquisition cadence), not veracity core — genuinely applied: the next tick
    # reads this flag and skips. No sign-off gate.
    new_paused = paused == "true"
    pub = await _publish(
        events.ADMIN_CLOCK_SET,
        clock,
        events.admin_event(clock, reason=reason, clock=clock, paused=new_paused),
    )
    if not pub:
        msg = _BUS_DOWN
    else:
        msg = "Updates paused — the next pull will skip." if new_paused else "Updates are back on."
    return _redirect("/clocks", msg)


@app.get("/config", response_class=HTMLResponse)
async def config_view(ok: str = "") -> str:
    """F5 — show every tunable knob with its live default + any proposed (pending) override.

    For a pending weight proposal (#123) we also show the A/B-on-replay impact: Brier before/after
    and how many resolved facts would change verdict (promoted/demoted) — so a sign-off is informed
    by the downstream effect, not a raw number. Per-knob change history backs the revert control."""
    pool = app.state.pool
    rows = await pool.fetch(
        "select distinct on (data->>'key') data->>'key' k, data->>'value' v, "
        "data->>'reason' r, created_at from events where type = $1 "
        "order by data->>'key', id desc",
        events.ADMIN_THRESHOLD_CHANGED,
    )
    overrides = {r["k"]: {"value": r["v"], "reason": r["r"], "at": r["created_at"]} for r in rows}
    # Per-knob change history (latest first) — both proposals and reverts, for the audit trail.
    hist_rows = await pool.fetch(
        "select data->>'key' k, data->>'value' v, data->>'actor' a, type, created_at "
        "from events where type = any($1::text[]) order by id desc limit 200",
        [events.ADMIN_THRESHOLD_CHANGED, ADMIN_THRESHOLD_REVERTED],
    )
    history: dict[str, list] = {}
    for r in hist_rows:
        history.setdefault(r["k"], []).append(
            {"value": r["v"], "actor": r["a"], "reverted": r["type"] == ADMIN_THRESHOLD_REVERTED,
             "at": r["created_at"]}
        )
    replay = await replay_for_overrides(pool, overrides)
    # Active (promoted) config — what the pipeline actually reads now (#183/#184).
    promoted_rows = await pool.fetch(
        "select data from events where type = $1 order by id", events.ADMIN_CONFIG_PROMOTED
    )
    active = config.active_config(
        (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]) for r in promoted_rows
    )
    return _doc(_config_page(overrides, replay, history, active), "config", "config", flash=ok)


@app.post("/config/set")
async def config_set(key: str = Form(...), value: str = Form(...), reason: str = Form("")):
    # A proposal only — recorded + audited, never auto-applied. Promotion to live (esp. core
    # knobs: gate floor, scoring, judge model) needs sign-off + A/B-on-replay (D18 / §5).
    if key in config.KNOBS_BY_KEY and value.strip():
        pub = await _publish(
            events.ADMIN_THRESHOLD_CHANGED,
            key,
            events.admin_event(key, reason=reason, key=key, value=value.strip()),
        )
        msg = "Saved as a suggestion. Nothing changed live until it's reviewed." if pub else _BUS_DOWN
    else:
        msg = "No change — pick a setting and enter a value."
    return _redirect("/config", msg)


@app.post("/config/promote")
async def config_promote(key: str = Form(...), value: str = Form(...), reason: str = Form("")):
    # #184 — sign off a proposed override into the LIVE pipeline (admin.config.promoted, which the
    # corroborate agent folds via config.active_config). Only enactable knobs (those the pipeline
    # actually reads today) can be promoted; model routing / weights / tier cut-points aren't wired.
    if key not in config._ENACTABLE or not value.strip():
        return _redirect("/config", "That setting isn't wired into the pipeline yet — can't promote.")
    pub = await _publish(
        events.ADMIN_CONFIG_PROMOTED,
        key,
        events.admin_event(key, reason=reason, key=key, value=value.strip()),
    )
    msg = (
        f"Promoted — {key} = {value.strip()} is now live (takes effect on the next corroboration pass)."
        if pub
        else _BUS_DOWN
    )
    return _redirect("/config", msg)


@app.get("/sources", response_class=HTMLResponse)
async def sources_view(ok: str = "") -> str:
    """A2 — source registry off ingested articles + operator allow/deny + ownership grouping."""
    pool = app.state.pool
    srcs = await pool.fetch(
        "select source, count(*) n, max(ingested_at) last, array_agg(distinct language) langs "
        "from articles where source is not null group by source order by n desc"
    )
    id_to_source = {a["id"]: a["source"] for a in await pool.fetch("select id, source from articles")}
    clusters = [dict(c) for c in await pool.fetch("select originators from clusters")]
    wire = wire_collapsed_sources(clusters, id_to_source)
    flags = await pool.fetch(
        "select distinct on (data->>'source') data->>'source' s, data->>'status' st, "
        "data->>'reason' r from events where type = $1 order by data->>'source', id desc",
        events.ADMIN_SOURCE_FLAGGED,
    )
    grps = await pool.fetch(
        "select distinct on (data->>'source') data->>'source' s, data->>'group' g "
        "from events where type = $1 order by data->>'source', id desc",
        events.ADMIN_SOURCE_GROUPED,
    )
    flag_by = {r["s"]: {"status": r["st"], "reason": r["r"]} for r in flags}
    group_by = {r["s"]: r["g"] for r in grps}
    reg_rows = await pool.fetch(
        "select data from events where type in ($1, $2) order by id",
        events.SOURCE_REGISTERED, events.SOURCE_STATE_CHANGED,
    )
    registry = sreg.fold_sources(
        json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in reg_rows
    )
    return _doc(_sources_page(srcs, wire, flag_by, group_by, registry),
                "Every outlet Maat reads, and how you want each one treated", "sources", flash=ok)


@app.post("/sources/flag")
async def source_flag(source: str = Form(...), deny: str = Form("0"), reason: str = Form("")):
    # One on/off control: the /sources toggle posts deny=1 (deny) or omits it (allow / un-deny).
    status = "deny" if deny == "1" else "allow"
    pub = await _publish(
        events.ADMIN_SOURCE_FLAGGED,
        source,
        events.admin_event(source, reason=reason, source=source, status=status),
    )
    if not pub:
        msg = _BUS_DOWN
    elif status == "deny":
        msg = f"Denied {source} — stories sourced only from it are now dropped from the feed."
    else:
        msg = f"Allowed {source} — it can surface in the feed again."
    return _redirect("/sources", msg)


@app.post("/sources/group")
async def source_group(source: str = Form(...), group: str = Form(...), reason: str = Form("")):
    msg = ""
    if group.strip():
        pub = await _publish(
            events.ADMIN_SOURCE_GROUPED,
            source,
            events.admin_event(source, reason=reason, source=source, group=group.strip()),
        )
        msg = (
            f"Grouped {source} as '{group.strip()}' — its outlets now count as one source in "
            "corroboration." if pub else _BUS_DOWN
        )
    return _redirect("/sources", msg)


@app.get("/prompts", response_class=HTMLResponse)
async def prompts_view(key: str = "", ok: str = "") -> str:
    """P8 — edit the agent prompts directly. Edits go live on the next run; versioned + rollback.
    Three-panel layout: agents on the left, the selected agent's editor in the middle, the
    always-open Claude chat on the right. ``key`` selects the agent (defaults to the first editable)."""
    try:
        rows = await app.state.pool.fetch(
            "select key, version, text, active, reason, created_at from prompts order by key, version desc"
        )
        store_ready = True
    except asyncpg.UndefinedTableError:  # migration not applied yet — show built-ins, don't 500
        rows, store_ready = [], False
    by_key: dict[str, list] = {}
    for r in rows:
        by_key.setdefault(r["key"], []).append(r)
    selected = key if key in prompts.PROMPTS_BY_KEY else next(
        (p["key"] for p in prompts.PROMPTS if p["status"] == "active"), prompts.PROMPTS[0]["key"]
    )
    # #189: which prompts still carry the "needs review" tag (draft-seed + not yet marked reviewed).
    # Informational only — reads the events log in one pass, falling back to "all drafts" if absent.
    review = await prompts.review_map(app.state.pool)
    return _doc(_prompts_page(by_key, store_ready, selected, review), "prompts", "prompts", flash=ok)


@app.post("/prompts/save")
async def prompts_save(key: str = Form(...), text: str = Form(...), reason: str = Form("")):
    if key not in prompts.EDITABLE_KEYS:  # draft / on-device prompts are read-only
        return _redirect("/prompts", "Unknown prompt.")
    missing = prompts.missing_placeholders(key, text)
    if missing:  # safety: dropping a placeholder would break the run — refuse the save
        return _redirect("/prompts", f"Not saved — keep these placeholders: {' '.join(missing)}")
    pub = await _publish(
        events.ADMIN_PROMPT_UPDATED, key, events.admin_event(key, reason=reason, key=key, text=text)
    )
    msg = "Saved — live on the next run. Run the checks on Quality." if pub else _BUS_DOWN
    return _redirect("/prompts", msg)


@app.post("/prompts/restore")
async def prompts_restore(key: str = Form(...), reason: str = Form(""), text: str = Form("")):
    if key not in prompts.EDITABLE_KEYS:  # draft / on-device prompts are read-only
        return _redirect("/prompts", "Unknown prompt.")
    pub = await _publish(
        events.ADMIN_PROMPT_UPDATED,
        key,
        events.admin_event(key, reason=reason or "restore code default", key=key,
                           text=prompts.seed_default(key)),
    )
    msg = "Restored the original — live on the next run." if pub else _BUS_DOWN
    return _redirect("/prompts", msg)


@app.post("/prompts/rollback")
async def prompts_rollback(key: str = Form(...), version: int = Form(...)):
    if key not in prompts.EDITABLE_KEYS:  # draft / on-device prompts are read-only
        return _redirect("/prompts", "Unknown prompt.")
    row = await app.state.pool.fetchrow(
        "select text from prompts where key = $1 and version = $2", key, version
    )
    if row is None:
        return _redirect("/prompts", "That version no longer exists.")
    pub = await _publish(
        events.ADMIN_PROMPT_UPDATED,
        key,
        events.admin_event(key, reason=f"rolled back to v{version}", key=key, text=row["text"]),
    )
    msg = f"Rolled back to v{version} — live on the next run." if pub else _BUS_DOWN
    return _redirect("/prompts", msg)


@app.post("/prompts/reviewed")
async def prompts_reviewed(key: str = Form(...), reason: str = Form("")):
    """#189 — clear the "needs review" tag on a draft prompt. Publishes admin.prompt.reviewed;
    ``prompts.needs_review`` reads it back so the badge clears. Informational only — prompts are
    already live, this changes nothing about whether any path runs. On-device is rejected."""
    if key not in prompts.EDITABLE_KEYS:  # on-device prompts (Swift mirrors) carry no review action
        return _redirect("/prompts", "This prompt can't be reviewed here.")
    pub = await _publish(
        events.ADMIN_PROMPT_REVIEWED, key,
        events.admin_event(key, reason=reason or "reviewed", key=key),
    )
    msg = "Marked reviewed — the tag is cleared." if pub else _BUS_DOWN
    return _redirect(f"/prompts?key={quote(key)}", msg)


@app.post("/prompts/test")
async def prompts_test(key: str = Form(...), text: str = Form(...)):
    """Eval-on-change: run the golden corpus with this candidate text (other stages stay on their
    active prompts) and report pass/fail — before the operator relies on it. Live LLM calls."""
    if key not in prompts.GOLDEN_EVAL_KEYS:  # only the golden-eval prompts have fixtures to test on
        return _redirect("/prompts", "This prompt has no golden tests.")
    three = {}
    for k in ("extract", "classify", "extremity"):
        three[k] = text if k == key else await prompts.active_text(
            app.state.pool, k, prompts.seed_default(k)
        )
    try:
        report = await asyncio.to_thread(
            eval_goldens,
            extract_prompt=three["extract"],
            classify_prompt=three["classify"],
            extremity_prompt=three["extremity"],
        )
    except KeyError:  # no API key in the console env (e.g. the box deploy)
        return _redirect("/prompts", "Can't test here — API keys aren't set. Run `make eval-prompt` instead.")
    except Exception as exc:  # noqa: BLE001 - surface any pipeline failure to the operator
        return _redirect("/prompts", f"Test failed: {exc}")
    return _redirect("/prompts", f"Tested '{key}' on the goldens — {eval_prompt_summary(report)}")


class PromptChatMsg(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class PromptChatReq(BaseModel):
    key: str
    current: str  # the live editor text for THIS prompt (may be unsaved)
    messages: list[PromptChatMsg] = []  # the running conversation, oldest first


def _prompt_chat_prompt(label: str, purpose: str, current: str, messages: list[PromptChatMsg]) -> str:
    """Format the chat-agent instructions + the prompt under discussion + the conversation into the
    single string ``claude_complete`` takes. The prompt's own ``{placeholders}`` are inserted via
    ``replace`` (never ``str.format``) so tokens like ``{claim}`` survive verbatim. Pure."""
    head = (
        prompts.PROMPT_CHAT_AGENT
        .replace("{prompt_label}", label)
        .replace("{prompt_purpose}", purpose or "(no description on file)")
        .replace("{current_prompt}", current)
    )
    convo = "\n\n".join(
        f"{'cauri' if m.role == 'user' else 'You'}: {m.content}" for m in messages
    )
    return f"{head}\n\n--- Conversation so far ---\n{convo}\n\nYou:" if convo else head


@app.post("/prompts/chat")
async def prompts_chat(req: PromptChatReq) -> JSONResponse:
    """Raw-Claude chat helper for improving a prompt WITH cauri (#158). Sees the chat-agent
    instructions + the prompt's current editor text + the conversation; returns the assistant's
    reply as JSON. Degrades gracefully: a missing ANTHROPIC_API_KEY or any provider error returns
    HTTP 200 with an ``error`` field — the page keeps working, nothing 500s. Editable keys only.
    """
    p = prompts.PROMPTS_BY_KEY.get(req.key)
    if req.key not in prompts.EDITABLE_KEYS or p is None:  # draft / on-device are read-only here
        return JSONResponse({"error": "Chat is available on the editable prompts only."}, status_code=200)
    prompt = _prompt_chat_prompt(p["label"], p.get("description", ""), req.current, req.messages)
    try:
        reply = await asyncio.to_thread(
            seam.claude_complete, prompt, model=seam.CLAUDE_JUDGE, max_tokens=1500
        )
    except KeyError:  # ANTHROPIC_API_KEY not set (e.g. the box without the reader's key)
        return JSONResponse(
            {"error": "Chat unavailable — set ANTHROPIC_API_KEY in the reader's env to enable it."},
            status_code=200,
        )
    except Exception as exc:  # noqa: BLE001 - any provider/network error stays graceful, never 500
        return JSONResponse({"error": f"Chat unavailable — {exc}"}, status_code=200)
    return JSONResponse({"reply": reply.text})


async def _chat_ndjson(prompt: str, *, max_tokens: int):
    """Stream a Claude reply as newline-delimited JSON: ``{"t": "<delta>"}`` per token, then a
    terminal ``{"done": true}``; any provider error becomes one ``{"error": "..."}`` line instead of
    a 500 (the chat degrades in place, exactly like the non-streaming endpoint). JSON-encoding each
    delta keeps embedded newlines on a single NDJSON line, so the client can split on ``\\n``."""
    try:
        async for delta in seam.claude_stream(prompt, model=seam.CLAUDE_JUDGE, max_tokens=max_tokens):
            yield json.dumps({"t": delta}) + "\n"
        yield json.dumps({"done": True}) + "\n"
    except KeyError:  # ANTHROPIC_API_KEY not set
        yield json.dumps(
            {"error": "Chat unavailable — set ANTHROPIC_API_KEY in the reader's env to enable it."}
        ) + "\n"
    except Exception as exc:  # noqa: BLE001 - any provider/network error stays graceful, never 500
        yield json.dumps({"error": f"Chat unavailable — {exc}"}) + "\n"


@app.post("/prompts/chat/stream")
async def prompts_chat_stream(req: PromptChatReq) -> StreamingResponse:
    """Streaming variant of /prompts/chat (#158) — same prompt, same agent, token-by-token over
    NDJSON so the console chat renders as it generates. Editable keys only; degrades gracefully."""
    p = prompts.PROMPTS_BY_KEY.get(req.key)
    if req.key not in prompts.EDITABLE_KEYS or p is None:

        async def _guard():
            yield json.dumps({"error": "Chat is available on the editable prompts only."}) + "\n"

        return StreamingResponse(_guard(), media_type="application/x-ndjson")
    prompt = _prompt_chat_prompt(p["label"], p.get("description", ""), req.current, req.messages)
    return StreamingResponse(_chat_ndjson(prompt, max_tokens=1500), media_type="application/x-ndjson")


class AssistantReq(BaseModel):
    page: str = ""  # the current page's label, e.g. "Settings"
    purpose: str = ""  # what the page is for
    messages: list[PromptChatMsg] = []  # the running conversation, oldest first


def _assistant_prompt(page: str, purpose: str, messages: list[PromptChatMsg], system: str) -> str:
    """Fill the assistant's system prompt with the current page + append the conversation. Pure."""
    head = system.replace("{page}", page or "this page").replace(
        "{purpose}", purpose or "(no description on file)"
    )
    convo = "\n\n".join(
        f"{'Operator' if m.role == 'user' else 'You'}: {m.content}" for m in messages
    )
    return f"{head}\n\n--- Conversation so far ---\n{convo}\n\nYou:" if convo else head


@app.post("/assistant/chat")
async def assistant_chat(req: AssistantReq) -> JSONResponse:
    """Page-aware console assistant (the always-open right panel). Read-only Q&A for now — it
    explains the current page and how the console works. Its system prompt is the editable
    ``console_assistant`` prompt (operator-tunable on the Prompts page); ``{page}``/``{purpose}``
    are filled with the current page. Degrades gracefully (HTTP 200 + ``error``), never 500s."""
    system = await prompts.active_text(
        getattr(app.state, "pool", None), "console_assistant", prompts.CONSOLE_ASSISTANT
    )
    prompt = _assistant_prompt(req.page, req.purpose, req.messages, system)
    try:
        reply = await asyncio.to_thread(
            seam.claude_complete, prompt, model=seam.CLAUDE_JUDGE, max_tokens=1200
        )
    except KeyError:  # ANTHROPIC_API_KEY not set
        return JSONResponse(
            {"error": "Assistant unavailable — set ANTHROPIC_API_KEY in the reader's env to enable it."},
            status_code=200,
        )
    except Exception as exc:  # noqa: BLE001 - any provider/network error stays graceful, never 500
        return JSONResponse({"error": f"Assistant unavailable — {exc}"}, status_code=200)
    return JSONResponse({"reply": reply.text})


# ============================ routes: P8 dashboards (read-only, over real backends) ==========


async def _corroboration_history(pool) -> list[dict]:
    """The truth-over-time trajectory every learning fold (reputation, calibration, RL) reads:
    the `cluster_snapshots` projection, oldest→newest — falling back to the legacy
    `cluster.corroborated` stream until the first harvest. See `maat.learning.trajectory`."""
    return await load_trajectory(pool)


@app.get("/reputation", response_class=HTMLResponse)
async def reputation_view(ok: str = "") -> str:
    """A3 (#74) — source reputation as a time-trajectory fold over the corroboration history.

    Real `learning.reputation.fold_reputation` (NOT the provisional /api/sources proxy): per
    source, independent-originator rate, attribution quality, solo-extraordinary red flags, and
    confirmation/refutation outcomes where the trajectory resolved them.
    """
    history = await _corroboration_history(app.state.pool)
    reps = fold_reputation(history)
    return _doc(_reputation_page(reps, len(history)), "reputation", "reputation", flash=ok)


@app.get("/calibration", response_class=HTMLResponse)
async def calibration_view(ok: str = "") -> str:
    """A4b (#76) — calibration (Brier/reliability, #60), de-US-centering (#59), pipeline health
    (#61), as dashboards over the live projections. References the backends; never rebuilds them."""
    pool = app.state.pool
    history = await _corroboration_history(pool)
    status = production_calibration(history)
    # de-US: build SourceMeta per article (origin country guessed from source domain, language
    # straight off the article row). This measures the spread of what's been ingested (§8).
    arts = await pool.fetch("select source, language from articles")
    breakdown, geo_dist, lang_dist = de_us_breakdown(arts)
    # pipeline health off the event log + projections (pure rollup, #61)
    event_rows = [dict(r) for r in await pool.fetch("select type, created_at from events")]
    try:
        dead_rows = [
            dict(r)
            for r in await pool.fetch(
                "select type, error, created_at from dead_letters order by id desc"
            )
        ]
    except asyncpg.UndefinedTableError:  # migration not applied — degrade, don't 500
        dead_rows = []
    cl_rows = [
        dict(r)
        for r in await pool.fetch(
            "select confidence, independent_originators, has_primary, extremity from clusters"
        )
    ]
    counts = {
        "articles": await pool.fetchval("select count(*) from articles"),
        "claims": await pool.fetchval("select count(*) from claims"),
        "clusters": await pool.fetchval("select count(*) from clusters"),
    }
    health = pipeline_health(event_rows, dead_rows, counts, clusters=cl_rows)
    return _doc(
        _calibration_page(status, breakdown, geo_dist, lang_dist, health),
        "calibration",
        "calibration",
        flash=ok,
    )


@app.get("/review", response_class=HTMLResponse)
async def review_view(ok: str = "") -> str:
    """A5 (#77) — the feedback triage queue (#58). Items routed to `review` need an operator;
    `auto-fix` items are safe-to-PR. Untriaged items are classified live (pure rules) so the
    operator sees a working queue even before the batch agent has run. Feedback is UNTRUSTED
    input — coordinated bursts are flagged as a possible attack vector."""
    pool = app.state.pool
    try:
        review = await routed_queue(pool, route="review")
        autofix = await routed_queue(pool, route="auto-fix")
        submitted = await feedback_queue(pool)
    except asyncpg.UndefinedTableError:  # events table missing entirely
        review, autofix, submitted = [], [], []
    triaged_ids = {
        (it.get("triage") or {}).get("item_id") or it.get("item_id") for it in (review + autofix)
    }
    pending = [s for s in submitted if s.get("item_id") not in triaged_ids]
    fresh = [_triage_preview(s) for s in pending]
    return _doc(
        _review_page(review, autofix, fresh, coordinated_signal(submitted)),
        "review",
        "review",
        flash=ok,
    )


@app.post("/review/act")
async def review_act(item_id: str = Form(...), action: str = Form(...), reason: str = Form("")):
    # #188 — an operator resolves a review-queue item: record a feedback.triaged DECISION so it
    # leaves the open queue (resolve / dismiss / route to auto-fix). Untrusted input (#77): this is
    # the human deciding — never an auto-action.
    routes = {"resolve": "resolved", "dismiss": "dismissed", "fix": "auto-fix"}
    route = routes.get(action)
    if route is None:
        return _redirect("/review", "Unknown action.")
    await feedback_record_triage(
        app.state.pool, None, item_id=item_id, category="operator-decision",
        route=route, reason=reason or f"operator {action}", auto_fixable=(route == "auto-fix"),
    )
    return _redirect("/review", f"Item {action} recorded.")


@app.get("/policy", response_class=HTMLResponse)
async def policy_view(ok: str = "") -> str:
    """A6 (#78) — RL policy control + capability grants. `learning.rl.policy_step` proposes a
    bounded, sign-off-gated policy improvement (weights via A/B-on-replay + source preferences
    within the safe envelope); it is NEVER auto-applied. The capability grants below state which
    knobs an operator must approve vs which may auto-tune — bounded self-modification (§5)."""
    history = await _corroboration_history(app.state.pool)
    proposal = policy_step(history)
    return _doc(_policy_page(proposal, len(history)), "policy", "policy", flash=ok)


@app.post("/policy/approve")
async def policy_approve(reason: str = Form("")):
    # #186 — sign off the RL policy's weight changes into the live pipeline. Each targets a Config
    # knob, so promote it via the same admin.config.promoted path as /config (#184). Source-
    # preference changes and capability grants are NOT auto-applied here (they steer acquisition /
    # scope — a separate, deliberately-gated lever).
    proposal = policy_step(await _corroboration_history(app.state.pool))
    promoted = 0
    for ch in proposal.weight_changes:
        if ch.get("key") in config._ENACTABLE and ch.get("value") is not None:
            ok = await _publish(
                events.ADMIN_CONFIG_PROMOTED,
                ch["key"],
                events.admin_event(
                    ch["key"], actor="rl-policy",
                    reason=reason or ch.get("reason") or "RL policy approval",
                    key=ch["key"], value=str(ch["value"]),
                ),
            )
            promoted += int(bool(ok))
    if not proposal.weight_changes:
        msg = "No weight changes to approve."
    elif promoted:
        msg = (
            f"Approved — {promoted} weight change(s) promoted live (next corroboration pass). "
            "Source-preference changes are not auto-applied."
        )
    else:
        msg = _BUS_DOWN
    return _redirect("/policy", msg)


@app.post("/config/revert")
async def config_revert(key: str = Form(...), reason: str = Form("")):
    # Revert a knob to its live code default (#123): re-propose the default value as a normal
    # admin.threshold.changed proposal AND record an admin.threshold.reverted marker for the
    # audit trail. Still a proposal — promotion to live keeps the sign-off gate.
    knob = config.KNOBS_BY_KEY.get(key)
    if knob is None:
        return _redirect("/config", "Unknown setting.")
    default = str(knob["default"])
    pub = await _publish(
        ADMIN_THRESHOLD_REVERTED,
        key,
        events.admin_event(key, reason=reason or "revert to code default", key=key, value=default),
    )
    if pub:
        await _publish(
            events.ADMIN_THRESHOLD_CHANGED,
            key,
            events.admin_event(
                key, actor="revert", reason=reason or "revert to code default", key=key, value=default
            ),
        )
        if key in config._ENACTABLE:
            # #185: revert flows through the SAME enact path as promote — roll the live value back
            # to the code default (not just a re-proposal) for knobs the pipeline reads.
            await _publish(
                events.ADMIN_CONFIG_PROMOTED,
                key,
                events.admin_event(
                    key, actor="revert", reason="revert to code default", key=key, value=default
                ),
            )
            msg = f"Reverted {key} to its built-in {default} — live now (next corroboration pass)."
        else:
            msg = f"Reverted {key} to its built-in {default}. Filed as a suggestion — sign off to apply."
    else:
        msg = _BUS_DOWN
    return _redirect("/config", msg)


# ============================ routes: corrections (F3, admin events) =========================


@app.post("/claim/{clid}/correct")
async def correct_claim(
    clid: str,
    kind: str = Form(""),
    voice: str = Form(""),
    speaker: str = Form(""),
    reason: str = Form(""),
):
    fields: dict[str, str] = {}
    if kind in ("fact", "projection"):
        fields["kind"] = kind
    if voice in ("own", "attributed"):
        fields["voice"] = voice
    if speaker.strip():
        fields["speaker"] = speaker.strip()
    if fields:
        pub = await _publish(
            events.ADMIN_CLASSIFICATION_CORRECTED,
            clid,
            events.admin_event(clid, reason=reason, **fields),
        )
        msg = "Saved. This won't be overwritten when the pipeline re-runs." if pub else _BUS_DOWN
    else:
        msg = "No change — pick a new label first."
    return _redirect(f"/claim/{clid}", msg)


@app.post("/claim/{clid}/flag")
async def flag_claim(clid: str, abuse: str = Form(...), reason: str = Form("")):
    pub = await _publish(
        events.ADMIN_LAUNDERING_FLAGGED, clid, events.admin_event(clid, reason=reason, abuse=abuse)
    )
    msg = "Flagged. The outlet now owns this claim for scoring." if pub else _BUS_DOWN
    return _redirect(f"/claim/{clid}", msg)


@app.post("/cluster/{cid}/split")
async def split_cluster(cid: str, claim_ids: list[str] = Form(default=[]), reason: str = Form("")):
    pool = app.state.pool
    cl = await pool.fetchrow("select claim_ids, extremity from clusters where id = $1", cid)
    if cl is None:
        return _redirect("/", "That story no longer exists.")
    members = _jload(cl["claim_ids"])
    picked = set(claim_ids)
    selected = [m for m in members if m in picked]
    rest = [m for m in members if m not in picked]
    if not selected or not rest:  # a no-op split: leave the cluster intact
        return _redirect(f"/cluster/{cid}", "Tick at least one claim, and leave at least one, to split.")
    extremity = cl["extremity"] or "notable"
    new_ids: list[str] = []
    for part in (selected, rest):
        ncid = await _recorroborate(pool, part, extremity)
        if ncid:
            new_ids.append(ncid)
    await _publish("cluster.removed", cid, {"id": cid})
    await _publish(
        events.ADMIN_CLUSTER_SPLIT, cid, events.admin_event(cid, reason=reason, into=new_ids)
    )
    return _redirect("/audit", "Story split. Confidence recalculated for both parts.")


@app.post("/cluster/merge")
async def merge_clusters(cluster_ids: list[str] = Form(default=[]), reason: str = Form("")):
    pool = app.state.pool
    ids = [c for c in cluster_ids if c]
    if len(ids) < 2:
        return _redirect("/", "Pick another story to merge with.")
    rows = await pool.fetch(
        "select id, claim_ids, extremity from clusters where id = any($1::text[])", ids
    )
    if len(rows) < 2:
        return _redirect("/", "Those stories no longer exist.")
    order = {"ordinary": 0, "notable": 1, "extraordinary": 2}
    extremity = max((r["extremity"] or "notable" for r in rows), key=lambda e: order.get(e, 1))
    members: list[str] = []
    for r in rows:
        members.extend(_jload(r["claim_ids"]))
    members = list(dict.fromkeys(members))
    ncid = await _recorroborate(pool, members, extremity)
    for r in rows:
        if r["id"] != ncid:
            await _publish("cluster.removed", r["id"], {"id": r["id"]})
    await _publish(
        events.ADMIN_CLUSTER_MERGED, ncid or ids[0], events.admin_event(ncid or "", reason=reason, merged=ids)
    )
    return _redirect("/audit", "Stories merged. Confidence recalculated.")


@app.post("/cluster/{from_cid}/move")
async def move_claim(
    from_cid: str, claim_id: str = Form(...), to_cluster: str = Form(...), reason: str = Form("")
):
    pool = app.state.pool
    src = await pool.fetchrow("select claim_ids, extremity from clusters where id = $1", from_cid)
    dst = await pool.fetchrow("select claim_ids, extremity from clusters where id = $1", to_cluster)
    if src is None or dst is None:
        return _redirect(f"/cluster/{from_cid}", "That story no longer exists.")
    src_ids = [x for x in _jload(src["claim_ids"]) if x != claim_id]
    dst_ids = _jload(dst["claim_ids"])
    if claim_id not in dst_ids:
        dst_ids.append(claim_id)
    await _publish("cluster.removed", from_cid, {"id": from_cid})
    await _publish("cluster.removed", to_cluster, {"id": to_cluster})
    await _recorroborate(pool, src_ids, src["extremity"] or "notable")
    await _recorroborate(pool, dst_ids, dst["extremity"] or "notable")
    await _publish(
        events.ADMIN_CLAIM_MOVED,
        claim_id,
        events.admin_event(claim_id, reason=reason, from_cluster=from_cid, to_cluster=to_cluster),
    )
    return _redirect("/audit", "Claim moved. Both stories rescored.")


# ============================ event-publish + recompute glue ================================


async def _publish(type_: str, stream_id: str, data: dict) -> bool:
    """Publish an event to the bus; the kernel is the single writer that projects it."""
    nc = app.state.nats
    if nc is None:
        return False
    await events.publish(nc, type_, stream_id, data)
    await nc.flush()
    return True


async def _claimrows(pool, ids: list[str]) -> tuple[list[ClaimRow], dict[str, str]]:
    rows = await pool.fetch(
        "select c.id, c.text, c.article_id, a.source, a.body from claims c "
        "join articles a on a.id = c.article_id where c.id = any($1::uuid[])",
        ids,
    )
    claims = [
        ClaimRow(id=str(r["id"]), text=r["text"], article_id=r["article_id"], source=r["source"] or "")
        for r in rows
    ]
    bodies = {r["article_id"]: (r["body"] or "") for r in rows}
    return claims, bodies


async def _recorroborate(pool, ids: list[str], extremity: str) -> str | None:
    """Recompute a fixed claim set into a cluster and publish it (F3). Returns the new id."""
    if not ids:
        return None
    claims, bodies = await _claimrows(pool, ids)
    if not claims:
        return None
    corr = corroborate_fixed(claims, bodies, extremity)
    ncid = cluster_id(corr.claim_ids)
    await _publish("cluster.corroborated", ncid, _corr_payload(ncid, corr))
    return ncid


def _corr_payload(cid: str, corr) -> dict:
    return {
        "id": cid,
        "fact": corr.fact,
        "sources": corr.sources,
        "originators": corr.originators,
        "independent_originators": corr.independent_originators,
        "has_primary": corr.has_primary,
        "extremity": corr.extremity,
        "confidence": corr.confidence,
        "claim_ids": corr.claim_ids,
    }


# ============================ P8 dashboard data prep (pure where possible) ====================


def de_us_breakdown(articles) -> tuple[de_us.ScoreBreakdown, dict, dict]:
    """Build the de-US-centering read (§8, #59) from ingested-article rows.

    Each article becomes a `de_us.SourceMeta` — origin country guessed from the source domain
    (the feed's own best-effort TLD map), language straight off the row. Returns the per-axis
    score breakdown plus the geographic + language distributions for the dashboard. Pure."""
    from maat.serving.feed import _source_country  # reuse the TLD→country guess

    metas = [
        de_us.SourceMeta(
            source_country=(_source_country(a["source"] or "") or None),
            language=(a["language"] or None),
        )
        for a in articles
    ]
    return (
        de_us.score(metas),
        de_us.geographic_distribution(metas),
        de_us.language_distribution(metas),
    )


def coordinated_signal(submitted: list[dict], *, threshold: int = 5) -> dict:
    """Flag possible coordinated feedback — feedback is UNTRUSTED input (#77), so a burst from
    one source is a candidate attack vector, not a mandate. Counts items per `source`; any source
    at/above `threshold` is surfaced as suspicious. Pure (no DB)."""
    from collections import Counter

    by_source = Counter((s.get("source") or "unknown") for s in submitted)
    suspicious = {src: n for src, n in by_source.items() if n >= threshold}
    return {
        "total": len(submitted),
        "by_source": dict(by_source.most_common()),
        "suspicious": suspicious,
    }


def _triage_preview(item: dict) -> dict:
    """Classify an as-yet-untriaged feedback item with the pure rule-based classifier so the
    operator sees a working queue before the batch agent runs. No I/O, no LLM."""
    res = triage_classify(item.get("text", ""), item.get("category_hint", ""))
    return {
        "item_id": item.get("item_id", ""),
        "text": item.get("text", ""),
        "source": item.get("source", "unknown"),
        "submitted_at": item.get("submitted_at"),
        "category": res.category,
        "route": res.route,
        "confidence": res.confidence,
        "reason": res.reason,
        "auto_fixable": res.auto_fixable,
    }


# Knob keys whose proposals can be replayed through `Weights` (the calibration weight-set).
_REPLAYABLE = {
    "decay.routine", "decay.ordinary", "decay.notable", "decay.significant",
    "decay.extraordinary", "confidence.primary_lift", "confidence.cap",
}


def _weights_with_override(key: str, value: str) -> Weights | None:
    """Build a candidate `Weights` = defaults with one knob overridden, or None if the key
    isn't a weight knob / the value won't parse. Pure."""
    if key not in _REPLAYABLE:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    base = Weights.defaults()
    if key.startswith("decay."):
        level = key.split(".", 1)[1]
        if level not in base.decay:
            return None
        decay = dict(base.decay)
        decay[level] = v
        from dataclasses import replace

        return replace(base, decay=decay)
    if key == "confidence.primary_lift":
        from dataclasses import replace

        return replace(base, primary_lift=v)
    if key == "confidence.cap":
        from dataclasses import replace

        return replace(base, cap=v)
    return None


async def replay_for_overrides(pool, overrides: dict) -> dict:
    """For each pending weight proposal, run A/B-on-replay over the resolved history so the
    Config sign-off shows Brier before/after + N facts changing verdict (#123). Returns
    {key: ReplayAB}; skips knobs that aren't weight-replayable or won't parse."""
    candidates = {
        k: w
        for k, ov in overrides.items()
        if (w := _weights_with_override(k, ov.get("value", ""))) is not None
    }
    if not candidates:
        return {}
    from maat.learning.calibration import observations_from_history

    history = await _corroboration_history(pool)
    obs = observations_from_history(history)
    base = Weights.defaults()
    return {k: replay_ab(obs, base=base, candidate=w) for k, w in candidates.items()}


# ============================ pure helpers (rendering + derivation) ==========================


def _jload(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _jobj(v) -> dict:
    return json.loads(v) if isinstance(v, str) else (v or {})


def _rget(r, key, default=None):
    try:
        return r[key]
    except (KeyError, IndexError):
        return default


# ── JSON feed API (P5 #48, minimal) — the Apple client reads this ──────────────
#
# The story (a corroboration cluster, §5.5) is the unit: its confidence read (§5.6-5.7),
# its independent-originator collapse, and the claims that compose it. The Swift `Story`
# model mirrors this shape exactly. Reads the same projections as the HTML view.


async def _article_meta(pool) -> dict[str, dict]:
    rows = await pool.fetch("select id, source, language, title, url from articles")
    return {r["id"]: dict(r) for r in rows}


async def _claims_by_id(pool) -> dict[str, dict]:
    rows = await pool.fetch(
        "select id, article_id, voice, speaker, kind, is_synthesis, horizon, "
        "in_headline, evidence_span, text from claims"
    )
    return {str(r["id"]): dict(r) for r in rows}


def _origin_groups(cluster, meta: dict[str, dict]) -> list[dict]:
    groups = []
    for grp in _jload(cluster["originators"]):
        sources = sorted({(meta.get(a) or {}).get("source") or a for a in grp})
        groups.append({"sources": sources, "collapsed": len(grp) > 1})
    return groups


def _claim_json(c: dict, meta: dict[str, dict]) -> dict:
    a = meta.get(c["article_id"]) or {}
    return {
        "id": str(c["id"]),
        "text": c["text"],
        "voice": c["voice"],
        "speaker": c["speaker"],
        "kind": c["kind"],
        "is_synthesis": bool(c["is_synthesis"]),
        "horizon": c["horizon"],
        "in_headline": bool(c["in_headline"]),
        "evidence_span": c.get("evidence_span"),
        "article_id": c["article_id"],
        "source": a.get("source"),
        "language": a.get("language") or "en",
    }


async def _article_full_map(pool) -> dict[str, dict]:
    rows = await pool.fetch(
        "select id, source, title, url, language, body, ingested_at from articles"
    )
    return {r["id"]: dict(r) for r in rows}


def _article_json(a: dict) -> dict:
    ts = a.get("ingested_at")
    return {
        "id": a["id"],
        "source": a.get("source"),
        "title": a.get("title"),
        "body": a.get("body") or "",
        "url": a.get("url"),
        "language": a.get("language") or "en",
        "ingested_at": ts.isoformat() if ts else None,
    }


def _cluster_article_ids(cluster, claims_by_id: dict) -> list[str]:
    """Distinct article ids behind a cluster (the full texts a reader can read), in a stable order."""
    ids: list[str] = []
    seen: set[str] = set()
    for cid in (str(x) for x in _jload(cluster["claim_ids"])):
        c = claims_by_id.get(cid)
        if c and c["article_id"] not in seen:
            seen.add(c["article_id"])
            ids.append(c["article_id"])
    for grp in _jload(cluster["originators"]):
        for aid in grp:
            if aid not in seen:
                seen.add(aid)
                ids.append(aid)
    return ids


def _story_json(cluster, claims_by_id: dict, meta: dict[str, dict]) -> dict:
    claim_ids = [str(x) for x in _jload(cluster["claim_ids"])]
    claims = [_claim_json(claims_by_id[cid], meta) for cid in claim_ids if cid in claims_by_id]
    languages = sorted({c["language"] for c in claims}) or ["en"]
    return {
        "id": cluster["id"],
        "fact": cluster["fact"],
        "confidence": float(cluster["confidence"] or 0.0),
        "verdict": _confidence_label(
            float(cluster["confidence"] or 0.0),
            independent_originators=int(cluster["independent_originators"] or 0),
            has_primary=bool(cluster["has_primary"]),
            extremity=cluster["extremity"],
        )[0],
        "extremity": cluster["extremity"] or "notable",
        "independent_originators": int(cluster["independent_originators"] or 0),
        "has_primary": bool(cluster["has_primary"]),
        "source_count": len(_jload(cluster["sources"])),
        "originator_groups": _origin_groups(cluster, meta),
        "languages": languages,
        "claims": claims,
    }


# Feed + story are served by serving/feed.py's feed_router at /api/v2 (mounted at startup) — the
# canonical Feed API (#48, de-US ordered + confidence-labelled + article bodies) supersedes the
# earlier /api/feed and /api/story stubs that lived here.


class TranslateReq(BaseModel):
    text: str
    target: str = "en"
    source: str | None = None


@app.post("/api/translate")
async def api_translate(req: TranslateReq) -> JSONResponse:
    # Cloud fallback for §4 translate-for-display. The client translates ON-DEVICE first (Apple
    # Translation); it only calls this when the on-device pair is unavailable. Routes through the
    # provider seam (mistral_complete) — translate-for-display only, never scores a translation.
    # On any provider error (incl. no MISTRAL key) it returns the original text (engine=identity),
    # so the reader degrades gracefully and still runs keyless.
    translated, engine = await asyncio.to_thread(
        translate_text, req.text, req.target, req.source
    )
    return JSONResponse(
        {"translated": translated, "source": req.source, "target": req.target, "engine": engine}
    )


class FeedbackReq(BaseModel):
    text: str
    category_hint: str = ""
    source: str = "reader"
    story_id: str | None = None


@app.post("/api/feedback")
async def api_feedback(req: FeedbackReq) -> JSONResponse:
    # Reader feedback intake (#58): the front door the loop was missing. Publishes a
    # feedback.submitted event (feedback.record) that surfaces in the /review queue and that the
    # triage agent routes to review / auto-fix. Untrusted input (#77) — a coordinated burst is
    # surfaced on /review, never auto-actioned.
    if not req.text.strip():
        raise HTTPException(status_code=422, detail="empty feedback")
    hint = (req.category_hint + (f" story:{req.story_id}" if req.story_id else "")).strip()
    item_id = await feedback_record(
        app.state.pool, None, text=req.text, category_hint=hint, source=req.source
    )
    return JSONResponse({"item_id": item_id, "status": "submitted"})


# ── Source reputation (provisional, pre-#37) — the Apple client's Sources view reads this ──────────
#
# Reputation as a learned truthfulness fold is P3 (#37) and not built yet. Until it is, approximate a
# per-source signal from the corroboration projections: a source that keeps turning up in well-
# corroborated clusters, or carries primary-source standing, scores higher. This is a PROXY, clearly
# labelled `provisional` — NOT the §6 truthfulness trajectory. Cold-start sources stay neutral (§6.6).

_PRIMARY_MARKERS = (
    "statement", "communiqué", "communique", "ministry", "préfecture", "prefecture",
    "official", "dataset", "document", "filing", "registry", "gazette",
)


def _is_primary_name(name: str) -> bool:
    n = (name or "").lower()
    return any(m in n for m in _PRIMARY_MARKERS)


def _source_tier(reputation: float, cold_start: bool) -> str:
    """Reliability = truthfulness over time (BRIEF §6.2), in plain words. This is a SOURCE-level
    standing — never claim-level corroboration, and never "primary source" (a per-claim role)."""
    if cold_start:
        return "not yet rated"
    if reputation >= 0.85:
        return "highly reliable"
    if reputation >= 0.7:
        return "generally reliable"
    if reputation >= 0.5:
        return "mixed reliability"
    if reputation >= 0.3:
        return "generally unreliable"
    return "unreliable"


def _cluster_sources(cluster, art_source: dict, claim_art: dict) -> set[str]:
    srcs: set[str] = set()
    for cid in _jload(cluster["claim_ids"]):
        aid = claim_art.get(str(cid))
        if aid and art_source.get(aid):
            srcs.add(art_source[aid])
    for grp in _jload(cluster["originators"]):
        for aid in grp:
            if art_source.get(aid):
                srcs.add(art_source[aid])
    return srcs


async def _source_ratings(pool) -> list[dict]:
    """Per-source reputation as the REAL §6 truth-over-time fold (#192/#37), not the old proxy.

    Folds the ``cluster.corroborated`` event trajectory into per-source standing
    (``learning.reputation.fold_reputation``): independent-originator rate, confirmation/refutation
    outcomes where the trajectory resolved them, solo-extraordinary red flags. ``reputation`` is the
    outcome-anchored 0..1 collapse (``reputation_score``); ``trajectory`` is a real sparkline of
    that standing over expanding windows of history (``reputation_trajectories``) — replacing the
    previous last-N-confidences proxy. Sources acquired but never corroborated are listed as
    cold-start ("not yet rated"), so the Sources view keeps full coverage.
    """
    from collections import defaultdict

    history = await _corroboration_history(pool)
    reps = fold_reputation(history)
    rep_by_src = {r.source: r for r in reps}
    trajectories = reputation_trajectories(history)

    # Language metadata per source (reputation itself is outcome-based, language-agnostic).
    langrows = await pool.fetch(
        "select distinct source, language from articles where source is not null"
    )
    langs: dict[str, set] = defaultdict(set)
    all_sources: set[str] = set(rep_by_src)
    for r in langrows:
        all_sources.add(r["source"])
        if r["language"]:
            langs[r["source"]].add(r["language"])

    ratings = []
    for s in all_sources:
        rec = rep_by_src.get(s)
        primary_name = _is_primary_name(s)
        if rec is None:
            # Acquired but never in a corroborated cluster → genuinely not yet rated. Shown at the
            # neutral midpoint (BRIEF §6.6: cold-start sources are presented neutrally, never penalised).
            ratings.append(
                {
                    "name": s, "reputation": 0.5, "tier": _source_tier(0.5, True),
                    "is_primary": primary_name, "n_stories": 0, "cold_start": True,
                    "trajectory": [0.5], "languages": sorted(langs.get(s, set())) or ["en"],
                    "independent_rate": 0.0, "confirmed": 0, "refuted": 0, "unresolved": 0,
                    "confirmation_rate": None, "solo_extraordinary": 0,
                }
            )
            continue
        # Cold = no terminal truth outcome resolved yet (the truthfulness trajectory is unproven),
        # regardless of primary standing — which is surfaced separately via is_primary.
        cold = rec.outcome_n == 0
        score = reputation_score(rec)
        traj = trajectories.get(s) or [score]
        ratings.append(
            {
                "name": s,
                "reputation": score,
                "tier": _source_tier(score, cold),
                "is_primary": primary_name or rec.primary_appearances > 0,
                "n_stories": rec.appearances,
                "cold_start": cold,
                "trajectory": [round(v, 3) for v in traj],
                "languages": sorted(langs.get(s, set())) or ["en"],
                # Real §6 dimensions (additive — existing clients ignore unknown keys).
                "independent_rate": rec.independent_rate,
                "confirmed": rec.facts_confirmed,
                "refuted": rec.facts_refuted,
                "unresolved": rec.facts_unresolved,
                "confirmation_rate": rec.confirmation_rate,
                "solo_extraordinary": rec.solo_extraordinary,
            }
        )
    ratings.sort(key=lambda r: (r["cold_start"], -r["reputation"], r["name"]))
    return ratings


@app.get("/api/sources")
async def api_sources() -> JSONResponse:
    ratings = await _source_ratings(app.state.pool)
    return JSONResponse(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "provisional": False,
            "note": "Reputation is the §6 truth-over-time fold over the corroboration trajectory "
            "(confirmation/refutation outcomes + independent-originator rate); `trajectory` is the "
            "sparkline of that standing across history. Sources never corroborated show as cold-start.",
            "count": len(ratings),
            "sources": ratings,
        }
    )


@app.get("/api/source/{name}")
async def api_source(name: str) -> JSONResponse:
    pool = app.state.pool
    match = next((r for r in await _source_ratings(pool) if r["name"] == name), None)
    if match is None:
        raise HTTPException(status_code=404, detail="no such source")
    arts = await pool.fetch("select id, source from articles")
    claims = await pool.fetch("select id, article_id from claims")
    art_source = {a["id"]: a["source"] for a in arts}
    claim_art = {str(c["id"]): c["article_id"] for c in claims}
    clusters = await pool.fetch(
        "select id, fact, confidence, originators, claim_ids from clusters order by confidence desc"
    )
    stories = [
        {"id": cl["id"], "fact": cl["fact"], "confidence": float(cl["confidence"] or 0.0)}
        for cl in clusters
        if name in _cluster_sources(cl, art_source, claim_art)
    ]
    return JSONResponse({**match, "stories": stories})


def _badge(text: str, cls: str, tip: str = "") -> str:
    t = f' title="{html.escape(tip)}"' if tip else ""
    return f'<span class="b {cls}"{t}>{html.escape(text)}</span>'


def derivation_explain(independent_originators: int, has_primary: bool, extremity: str) -> str:
    """Plain-language derivation of a confidence read (F2) — exactly tracks `confidence_read`.

    Spells out what drove the number so an operator can see (and challenge) the call:
    how many independent originators, the claim's prior, and whether a primary source lifted it.
    """
    conf = confidence_read(independent_originators, has_primary, extremity)
    plural = "s" if independent_originators != 1 else ""
    bits = [
        f"{independent_originators} independent originator{plural}",
        f"prior: {extremity}",
    ]
    if has_primary:
        bits.append("primary source (closes half the remaining gap)")
    return f"{' · '.join(bits)} → {round(conf * 100)}% confidence"


def _claim_badges(c) -> str:
    badges = []
    if c["in_headline"]:
        badges.append(_badge("headline", "head", "This claim appeared in the article's headline"))
    if c["voice"] == "attributed":
        badges.append(_badge(
            f"quoted · {c['speaker'] or '?'}", "attr",
            "Attributed — someone else said it. The outlet is judged on quoting accurately, "
            "not on whether the claim is true.",
        ))
    else:
        badges.append(_badge("outlet's own words", "own",
                             "The outlet stated this itself, so it's accountable for it"))
    if c["kind"] == "fact":
        badges.append(_badge("fact", "fact", "A claim about now, checkable as true or false"))
    elif c["kind"] == "projection":
        extra = f" · {c['horizon']}" if c["horizon"] else ""
        badges.append(_badge(f"prediction{extra}", "proj",
                             "A forecast about the future — scored separately, never as truth"))
    if c["is_synthesis"]:
        badges.append(_badge("conclusion", "syn",
                             "The outlet's own conclusion drawn from other claims"))
    if _rget(c, "corrected"):
        badges.append(_badge("you fixed this", "corr",
                             "You corrected this; the pipeline won't overwrite it"))
    if _rget(c, "laundering_flag"):
        badges.append(_badge(f"flagged · {c['laundering_flag']}", "laun",
                             "You flagged this as misleading attribution"))
    return "".join(badges)


def _claim(c) -> str:
    text = html.escape(c["text"])
    cid = _rget(c, "id")
    inner = f'<a class="clink" href="/claim/{cid}">{text}</a>' if cid else text
    return (
        f'<div class="claim"><div class="bs">{_claim_badges(c)}</div>'
        f'<div class="t">{inner}</div></div>'
    )


def _extlink(url, text, *, cls: str = "") -> str:
    """An external link that opens in a NEW TAB, or just the escaped text when there's no URL."""
    t = html.escape(text or "")
    u = (url or "").strip()
    if not u:
        return t
    c = f' class="{cls}"' if cls else ""
    return f'<a{c} href="{html.escape(u)}" target="_blank" rel="noopener noreferrer">{t}</a>'


def _card(a, claims, title_en: str = "") -> str:
    rows = "".join(_claim(c) for c in claims) or '<div class="claim t muted">no claims</div>'
    te = (title_en or "").strip()
    gloss = (
        f'<div class="title-en mut" lang="en" title="English translation — display only, never scored">{html.escape(te)}</div>'
        if te and te != (a["title"] or "").strip() else ""
    )
    return (
        f'<article class="card"><div class="src">{html.escape(a["source"] or "")}</div>'
        f'<h2>{_extlink(a.get("url"), a["title"])}</h2>{gloss}'
        f'<div class="claims">{rows}</div>'
        f'<div class="foot">{len(claims)} claims</div></article>'
    )


def _cluster_articles(cl) -> set[str]:
    arts: set[str] = set()
    for grp in _jload(cl["originators"]):
        arts.update(grp)
    return arts


def _group_stories(clusters) -> list[list]:
    """Roll corroborated facts up into stories (§5.7): clusters whose source articles overlap
    are one story. Render-time grouping for now; a story projection lands with the P4 graph.
    Within a story the headline is the most-asserted claim (most sources), then confidence."""
    n = len(clusters)
    arts = [_cluster_articles(c) for c in clusters]
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if arts[i] & arts[j]:
                parent[find(i)] = find(j)
    groups: dict[int, list] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(clusters[i])
    stories = [
        sorted(g, key=lambda c: (len(_jload(c["sources"])), float(c["confidence"] or 0)), reverse=True)
        for g in groups.values()
    ]
    stories.sort(key=lambda g: len(_jload(g[0]["sources"])), reverse=True)
    return stories


def _conf_bar(cl) -> str:
    conf = float(cl["confidence"] or 0.0)
    pct = round(conf * 100)
    label, tier = _confidence_label(
        conf,
        independent_originators=cl["independent_originators"],
        has_primary=cl["has_primary"],
        extremity=cl["extremity"],
    )
    return (
        f'<div class="conf {tier}"><div class="cbar"><div class="cfill" style="width:{pct}%"></div></div>'
        f'<span class="cpct">{pct}%</span><span class="label {tier}">{html.escape(label)}</span></div>'
    )


def _originator_rows(cl, id_to_source) -> str:
    rows = []
    for grp in _jload(cl["originators"]):
        names = sorted({id_to_source.get(a, a) for a in grp})
        wire = len(grp) > 1
        lbl = "wire · collapsed" if wire else "independent"
        rows.append(
            f'<div class="orig {"wire" if wire else "indep"}">'
            f'<span class="ol" title="Wire = the same report reprinted across outlets (counted once, not separate confirmation). Independent = a distinct originator.">{lbl}</span>{html.escape(", ".join(names))}</div>'
        )
    return "".join(rows)


def _headline(cl, id_to_source) -> str:
    primary = _badge("primary source", "fact") if cl["has_primary"] else ""
    n_src = len(_jload(cl["sources"]))
    extremity = cl["extremity"] or "notable"
    ex_text = "extraordinary · bar raised" if extremity == "extraordinary" else f"{extremity} claim"
    ex_badge = f'<span class="ex {extremity}">{html.escape(ex_text)}</span>'
    cid = _rget(cl, "id")
    fact = html.escape(cl["fact"])
    fact_html = f'<a class="clink" href="/cluster/{cid}">{fact}</a>' if cid else fact
    return (
        f'<div class="cfact">{fact_html}</div>'
        f"{_conf_bar(cl)}"
        f'<div class="cmeta"><b>{n_src}</b> sources &rarr; '
        f'<b>{cl["independent_originators"]}</b> independent originators {ex_badge} {primary}</div>'
        f'<div class="origs">{_originator_rows(cl, id_to_source)}</div>'
    )


def _supporting(cl) -> str:
    conf = float(cl["confidence"] or 0.0)
    pct = round(conf * 100)
    _, tier = _confidence_label(conf)
    cid = _rget(cl, "id")
    fact = html.escape(cl["fact"])
    fact_html = f'<a class="clink" href="/cluster/{cid}">{fact}</a>' if cid else fact
    return (
        f'<div class="sup"><span class="sup-pct {tier}">{pct}%</span>'
        f'<span class="sup-fact">{fact_html}</span></div>'
    )


def _story(group, id_to_source) -> str:
    head = _headline(group[0], id_to_source)
    sup = ""
    if len(group) > 1:
        items = "".join(_supporting(c) for c in group[1:])
        sup = f'<div class="sup-wrap"><div class="sup-head">Also corroborated in this story</div>{items}</div>'
    return f'<div class="story">{head}{sup}</div>'


def _feed_page(articles, by_article, clusters, id_to_source, title_en=None, flash: str = "") -> str:
    panel = ""
    n_stories = 0
    if clusters:
        stories = _group_stories(clusters)
        n_stories = len(stories)
        items = "".join(_story(g, id_to_source) for g in stories)
        panel = (
            '<section class="panel"><h3>Stories · corroboration over spread, confidence on '
            f"every claim</h3>{items}</section>"
        )
    cards = "".join(_card(a, by_article.get(a["id"], []), (title_en or {}).get(a["id"], "")) for a in articles)
    if not cards:
        cards = '<p class="empty">No stories yet — pull some news from the Updates tab.</p>'
    subtitle = f"{n_stories or len(articles)} stories · corroboration over spread · confidence on every claim"
    return _doc(panel + cards, subtitle, "content", flash=flash)


# --- inspectors (F2) + correction forms (F3) ---


def _opts(others) -> str:
    return "".join(
        f'<option value="{html.escape(o["id"])}">{html.escape((o["fact"] or "")[:60])}</option>'
        for o in others
    )


def _cluster_page(cl, members, id_to_source, others) -> str:
    deriv = derivation_explain(
        cl["independent_originators"], cl["has_primary"], cl["extremity"] or "notable"
    )
    cid = cl["id"]
    checks = "".join(
        f'<label class="ck"><input type="checkbox" name="claim_ids" value="{m["id"]}"> '
        f'{html.escape(m["text"])}</label>'
        for m in members
    )
    split = (
        f'<form class="box" method="post" action="/cluster/{cid}/split">'
        '<div class="bl">Split this story</div>'
        '<div class="note">Tick the claims that don\'t belong here — they move into a new, separate '
        'story. Confidence is recalculated for both.</div>'
        f'{checks}'
        '<input class="reason" name="reason" placeholder="reason (optional, saved to History)">'
        '<button title="Move the ticked claims into a new story; both stories are rescored">'
        'Split off ticked claims</button></form>'
    )
    merge = (
        '<form class="box" method="post" action="/cluster/merge">'
        f'<input type="hidden" name="cluster_ids" value="{html.escape(cid)}">'
        '<div class="bl">Merge with another story</div>'
        '<div class="note">Pick another story that is really the same event. They become one; '
        'confidence is recalculated for the result.</div>'
        f'<select name="cluster_ids">{_opts(others)}</select>'
        '<input class="reason" name="reason" placeholder="reason (optional)">'
        '<button title="Combine these into one story and rescore">Merge stories</button></form>'
        if others else ""
    )
    mrows = []
    for m in members:
        move = (
            f'<form class="inline" method="post" action="/cluster/{cid}/move">'
            f'<input type="hidden" name="claim_id" value="{m["id"]}">'
            f'<select name="to_cluster">{_opts(others)}</select>'
            '<input class="reason" name="reason" placeholder="reason">'
            '<button title="Move just this claim to another story; both are rescored">Move</button>'
            '</form>'
            if others else ""
        )
        mrows.append(
            f'<div class="mc"><div class="bs">{_claim_badges(m)}</div>'
            f'<div class="t"><a class="clink" href="/claim/{m["id"]}">{html.escape(m["text"])}</a></div>'
            f'<div class="src">{html.escape(m["art_source"] or "")}</div>{move}</div>'
        )
    return (
        '<div class="ins">'
        f'{_headline(cl, id_to_source)}'
        f'<div class="deriv" title="The same calculation that produced the score above">'
        f'How this score was reached: {html.escape(deriv)}</div>'
        f'{split}{merge}'
        f'<div class="bl mt">Claims in this story ({len(members)})</div>{"".join(mrows)}</div>'
    )


def _claim_page(c, prov) -> str:
    clid = c["id"]
    relay = _jload(c["relay_chain"])
    relay_html = " → ".join(html.escape(str(x)) for x in relay) if relay else "—"
    prov_rows = "".join(
        f'<li><span class="mono">{html.escape(p["type"])}</span> '
        f'<span class="mut">{p["created_at"]:%Y-%m-%d %H:%M}</span></li>'
        for p in prov
    )
    meta = (
        f'<div class="kv"><b>From article</b> {_extlink(c.get("art_url"), c["art_title"])} '
        f'<span class="mut">({html.escape(c["art_source"] or "")}, {html.escape(c["art_language"] or "?")})</span></div>'
        f'<div class="kv" title="The exact words this claim was pulled from">'
        f'<b>Exact wording</b> {html.escape(c["evidence_span"] or "—")}</div>'
        f'<div class="kv" title="Who passed the claim along, in order">'
        f'<b>Quote chain</b> {relay_html}</div>'
    )
    correct = (
        f'<form class="box" method="post" action="/claim/{clid}/correct">'
        '<div class="bl">Fix this claim</div>'
        '<div class="note">Change how Maat labelled it. Your fix is saved and won\'t be overwritten '
        'when the pipeline runs again.</div>'
        '<label title="Fact = checkable now · Prediction = about the future">Type '
        '<select name="kind"><option value="">— leave as is —</option>'
        '<option value="fact">fact</option><option value="projection">prediction</option></select></label>'
        '<label title="Outlet\'s own = it said it · Quoted = it quoted someone">Voice '
        '<select name="voice"><option value="">— leave as is —</option>'
        '<option value="own">outlet\'s own</option><option value="attributed">quoted</option>'
        '</select></label>'
        '<label>Speaker <input name="speaker" placeholder="(leave blank to keep)"></label>'
        '<input class="reason" name="reason" placeholder="reason (optional, saved to History)">'
        '<button title="Saves your label; it survives pipeline re-runs">Save fix</button></form>'
    )
    flag = (
        f'<form class="box" method="post" action="/claim/{clid}/flag">'
        '<div class="bl">Flag misleading attribution</div>'
        '<div class="note">Use when an outlet hides its own claim behind a quote. Flagging makes the '
        'outlet accountable for the claim.</div>'
        '<select name="abuse">'
        '<option value="endorsement">Endorsed it as true</option>'
        '<option value="bare_repetition">Stated it as its own fact</option>'
        '<option value="selective_amplification">Only ever amplifies one side</option></select>'
        '<input class="reason" name="reason" placeholder="reason (optional)">'
        '<button title="Makes the outlet own this claim for scoring">Flag it</button></form>'
    )
    provenance = (
        '<div class="bl mt">Where this came from</div>'
        f'<ul class="prov">{prov_rows or "<li class=mut>none</li>"}</ul>'
        '<div class="mut sm">Maat doesn\'t record which AI model judged this yet.</div>'
    )
    return (
        '<div class="ins">'
        f'<div class="cfact">{html.escape(c["text"])}</div>'
        f'<div class="bs">{_claim_badges(c)}</div>{meta}'
        f'{correct}{flag}{provenance}</div>'
    )


_ACTION_LABELS = {
    "admin.classification.corrected": "fixed a claim",
    "admin.laundering.flagged": "flagged attribution",
    "admin.cluster.split": "split a story",
    "admin.cluster.merged": "merged stories",
    "admin.claim.moved": "moved a claim",
    "admin.threshold.changed": "suggested a setting",
    "admin.run.triggered": "logged a run",
    "admin.source.flagged": "flagged a source",
    "admin.source.grouped": "grouped sources",
    "admin.clock.set": "paused/resumed updates",
    "admin.prompt.updated": "edited a prompt",
    "admin.threshold.reverted": "reverted a setting",
}


def _action_label(event_type: str) -> str:
    """A plain-English name for an admin event type (History page). Pure."""
    return _ACTION_LABELS.get(event_type, event_type.removeprefix("admin."))


def _audit_page(rows) -> str:
    if not rows:
        return (
            '<div class="ins">'
            '<p class="empty">No changes yet. Anything you change in the console will be logged here.</p></div>'
        )
    trs = []
    for r in rows:
        d = _jobj(r["data"])
        extras = {k: v for k, v in d.items() if k not in ("target", "actor", "reason")}
        ex = ", ".join(f"{k}={v}" for k, v in extras.items())
        trs.append(
            "<tr>"
            f'<td class="mut">{r["created_at"]:%Y-%m-%d %H:%M}</td>'
            f'<td><span class="atype">{html.escape(_action_label(r["type"]))}</span></td>'
            f'<td class="mono">{html.escape(str(d.get("target", "")))}</td>'
            f'<td>{html.escape(str(d.get("actor", "")))}</td>'
            f'<td>{html.escape(str(d.get("reason", "")))}</td>'
            f'<td class="mono">{html.escape(ex)}</td></tr>'
        )
    return (
        '<div class="ins">'
        ''
        '<table class="aud"><tr><th>when</th><th>what</th><th>item</th><th>who</th>'
        f'<th>why</th><th>details</th></tr>{"".join(trs)}</table></div>'
    )


_STAGES = [
    ("Find articles", "article.ingested", "make acquire QUERY=… N=12  ·  make ingest-corpus"),
    ("Pull out claims", "claims.extracted", "make agents"),
    ("Label claims", "claims.classified", "make agents"),
    ("Score corroboration", "cluster.corroborated", "make corroborate"),
]

# ── Run the pipeline: one click runs the WHOLE flow with live per-step progress ──────────────
# acquire (scripts/clock.py) → extract+classify drain (the always-on agents process the new
# articles) → corroborate (recompute clusters). Each runs as a subprocess in the reader's env;
# progress lives in _RUN, polled by /runs/status. A DAILY SPEND CAP (#195, cauri: default $5/day,
# MAAT_DAILY_CAP_USD) gates the START of a run: once today's estimated LLM spend reaches the cap,
# the console refuses to kick off another run so a stuck loop can't burn the budget.
_RUN: dict = {"active": False, "started": None, "finished": None, "error": None, "steps": []}
_ROOT = Path(__file__).resolve().parents[2]  # the python/ project root (has scripts/ + maat/)


def _run_state() -> dict:
    """Run state with the 4 display steps always present (idle until a run starts)."""
    if not _RUN["steps"]:
        _RUN["steps"] = [{"label": lbl, "status": "idle"} for lbl, _e, _c in _STAGES]
    return _RUN


async def _run_proc(args: list[str]) -> None:
    """Run a pipeline command as a subprocess in the reader's env; raise on a non-zero exit."""
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(_ROOT),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        msg = (err or b"").decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(msg or f"exited {proc.returncode}")


async def _drain(max_s: int = 180, quiet_s: int = 15) -> None:
    """Let the always-on extract+classify agents catch up on the freshly acquired articles: poll
    the claims count, stop once it has been steady for `quiet_s` (or `max_s` elapses)."""
    pool = getattr(app.state, "pool", None)
    if pool is None:
        await asyncio.sleep(min(max_s, 20))
        return

    async def _count():
        try:
            return await pool.fetchval("select count(*) from claims")
        except Exception:  # noqa: BLE001
            return None

    prev, last_change, deadline = await _count(), time.monotonic(), time.monotonic() + max_s
    while time.monotonic() < deadline:
        await asyncio.sleep(5)
        cur = await _count()
        if cur != prev:
            prev, last_change = cur, time.monotonic()
        elif time.monotonic() - last_change >= quiet_s:
            break


async def _run_pipeline() -> None:
    """Background task: run the full pipeline, updating _RUN per step as it goes."""
    steps = _run_state()["steps"]

    def mark(i, status):
        steps[i]["status"] = status

    try:
        await _publish(events.ADMIN_RUN_TRIGGERED, "pipeline",
                       events.admin_event("pipeline", reason="full pipeline run from console"))
        mark(0, "running")  # Find articles
        await _run_proc([sys.executable, "scripts/clock.py"])
        mark(0, "done")
        mark(1, "running")  # Pull out claims + Label claims — the agents drain automatically
        mark(2, "running")
        await _drain()
        mark(1, "done")
        mark(2, "done")
        mark(3, "running")  # Score corroboration
        await _run_proc([sys.executable, "-m", "maat.agents.corroborate_agent"])
        mark(3, "done")
    except Exception as exc:  # noqa: BLE001 - surface failure to the operator, never crash the app
        _RUN["error"] = str(exc)
        for s in steps:
            if s["status"] == "running":
                s["status"] = "error"
    finally:
        _RUN["active"] = False
        _RUN["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _today_spend_usd(pool) -> float:
    """Estimated USD spent on LLM pipeline calls TODAY (UTC), from today's event log — the daily
    cap's cost signal (#195). The dominant Haiku/Sonnet stages (extract/classify/extremity);
    embeddings are negligible and excluded. Same estimator as /spend, date-scoped. cat-cafe is exact."""
    counts = {
        r["type"]: r["n"]
        for r in await pool.fetch(
            "select type, count(*) n from events where type in "
            "('claims.extracted','claims.classified','cluster.corroborated') "
            "and created_at >= date_trunc('day', now() at time zone 'utc') group by type"
        )
    }
    _, total = spend_mod.estimate_llm_spend(
        extract_calls=counts.get("claims.extracted", 0),
        classify_calls=counts.get("claims.classified", 0),
        extremity_calls=counts.get("cluster.corroborated", 0),
        embed_claims=0,
    )
    return total


@app.post("/runs/run-all")
async def runs_run_all() -> JSONResponse:
    """Kick off a full pipeline run (one at a time). Returns immediately; the UI polls /runs/status.

    Budget guard (#195): refuses to start once today's estimated spend has reached the daily cap
    (default $5, MAAT_DAILY_CAP_USD); the refusal is recorded in the audit log and surfaced to the
    operator via the run state's error line."""
    if _RUN["active"]:
        return JSONResponse({"already_running": True, **_run_state()})
    today = await _today_spend_usd(app.state.pool)
    cap = spend_mod.daily_cap_usd()
    budget = spend_mod.cap_status(today, cap)
    if not budget["allowed"]:
        await _publish(
            events.ADMIN_RUN_TRIGGERED, "pipeline",
            events.admin_event(
                "pipeline", reason=f"blocked by daily cap ${cap:.2f} (today ≈ ${today:.2f})",
                blocked=True,
            ),
        )
        _RUN.update(
            active=False,
            error=f"Daily spend cap ${cap:.2f} reached (today ≈ ${today:.2f}). Run skipped — "
            "raise MAAT_DAILY_CAP_USD or wait for tomorrow (UTC).",
            finished=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            steps=[{"label": lbl, "status": "idle"} for lbl, _e, _c in _STAGES],
        )
        return JSONResponse({"blocked": True, "budget": budget, **_run_state()})
    _RUN.update(
        active=True, started=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        finished=None, error=None, budget=budget,
        steps=[{"label": lbl, "status": "pending"} for lbl, _e, _c in _STAGES],
    )
    asyncio.create_task(_run_pipeline())
    return JSONResponse({"budget": budget, **_run_state()})


@app.get("/runs/status")
async def runs_status() -> JSONResponse:
    """Current pipeline-run state for the Activity progress strip."""
    return JSONResponse(_run_state())


def stage_summary(counts: dict) -> list[dict]:
    """Map event-type aggregates {type: {n, last}} to the pipeline stages (F4). Pure."""
    rows = []
    for label, etype, cmd in _STAGES:
        c = counts.get(etype) or {}
        rows.append(
            {"label": label, "type": etype, "cmd": cmd, "count": c.get("n", 0), "last": c.get("last")}
        )
    return rows


def _runs_page(stages, proj, recent, dead, dead_ready: bool = True) -> str:
    pcells = "".join(
        f'<div class="mcell"><div class="mk">{html.escape(k)}</div><div class="mv">{v}</div></div>'
        for k, v in proj.items()
    )
    srows = []
    for i, s in enumerate(stages):
        last = f'{s["last"]:%Y-%m-%d %H:%M}' if s["last"] else "never"
        srows.append(
            f'<div class="srow-step" title="event: {html.escape(s["type"])}">'
            f'<div class="sname">{html.escape(s["label"])}</div>'
            f'<div class="snum">{s["count"]}<span class="mut sm"> done · last {last}</span></div>'
            f'<span class="step-state" id="rs-{i}"></span></div>'
        )
    dead_html = ""
    if not dead_ready:
        dead_html = (
            '<div class="bl mt">Errors</div><div class="note">Not set up yet — restart the kernel '
            "(maat-kerneld) to apply the latest updates.</div>"
        )
    elif dead:
        drows = "".join(
            f'<tr><td class="mut">{r["created_at"]:%m-%d %H:%M}</td>'
            f'<td class="mono" style="color:#b3402e">{html.escape(r["type"])}</td>'
            f'<td class="mono">{html.escape(str(r["stream_id"] or ""))}</td>'
            f'<td class="mono">{html.escape((r["error"] or "")[:160])}</td></tr>'
            for r in dead
        )
        dead_html = (
            f'<div class="bl mt" title="Items that hit an error and were skipped, so they are not '
            f'lost silently">Errors — failed and skipped ({len(dead)})</div>'
            '<table class="aud"><tr><th>when</th><th title="Pipeline stage — the internal event name for what ran">step</th><th>item</th><th>error</th></tr>'
            f"{drows}</table>"
        )
    rrows = "".join(
        f'<tr><td class="mut">{r["created_at"]:%m-%d %H:%M}</td>'
        f'<td><span class="atype">{html.escape(r["type"])}</span></td>'
        f'<td class="mono">{html.escape(str(r["stream_id"] or ""))}</td></tr>'
        for r in recent
    )
    note = (
        '<div class="mut sm">Cost and timing for each AI call are tracked in cat-cafe (see the '
        "Quality tab). Total cost-per-run and re-building from scratch aren't wired up yet.</div>"
    )
    return (
        '<div class="ins">'
        ''
        f'<div class="mgrid">{pcells}</div>'
        '<div class="bl mt">Pipeline</div>'
        '<div class="runbar"><button id="run-btn" onclick="maatRunPipeline()">▶ Run the pipeline</button>'
        '<span class="run-status" id="run-status"></span></div>'
        f'{"".join(srows)}'
        f'{dead_html}<div class="bl mt">Recent activity</div>'
        f'<table class="aud"><tr><th>when</th><th title="Pipeline stage — the internal event name for what ran">step</th><th>item</th></tr>{rrows}</table>{note}</div>'
    )


def _spend_page(rows, llm_total: float, apify_usd, otlp: str, budget: dict | None = None,
                provider_spend=None) -> str:
    apify_cell = f"${apify_usd:,.2f}" if apify_usd is not None else "—"
    grand = llm_total + (apify_usd or 0.0)
    cells = (
        f'<div class="mcell"><div class="mk">Total (est.)</div><div class="mv">${grand:,.2f}</div></div>'
        f'<div class="mcell"><div class="mk">AI / LLM (est.)</div><div class="mv">${llm_total:,.2f}</div></div>'
        f'<div class="mcell"><div class="mk">Apify (actual)</div><div class="mv">{apify_cell}</div></div>'
    )
    budget_line = ""
    if budget:
        today = budget.get("today_usd", 0.0)
        if budget.get("capped"):
            cap = budget.get("cap_usd", 0.0)
            remaining = budget.get("remaining_usd", 0.0)
            state = (
                '<span class="b ok">runs allowed</span>' if budget.get("allowed")
                else '<span class="b warn">runs paused — cap reached</span>'
            )
            budget_line = (
                f'<div class="mut sm mt">Daily run cap (#195): today ≈ <b>${today:,.2f}</b> of '
                f'<b>${cap:,.2f}</b> — ${remaining:,.2f} left. {state}. '
                "Operator runs are blocked once today's estimate reaches the cap "
                "(<span class=\"mono\">MAAT_DAILY_CAP_USD</span>; UTC day).</div>"
            )
        else:
            budget_line = (
                f'<div class="mut sm mt">Daily run cap (#195): <b>uncapped</b> '
                f'(today ≈ ${today:,.2f}; set <span class="mono">MAAT_DAILY_CAP_USD</span> to enable).</div>'
            )
    srows = "".join(
        f'<tr><td>{html.escape(r.stage)}</td><td class="mono mut">{html.escape(r.model)}</td>'
        f'<td style="text-align:right">{r.calls:,}</td>'
        f'<td style="text-align:right">${r.usd:,.2f}</td></tr>'
        for r in rows
    )
    channel_block = ""
    if provider_spend:
        crows = "".join(
            f'<tr><td>{html.escape(r.provider)}</td>'
            f'<td style="text-align:right">{r.articles:,}</td>'
            f'<td style="text-align:right">${r.usd:,.2f}</td></tr>'
            for r in provider_spend
        )
        channel_block = (
            '<div class="bl mt">By acquisition channel (#241)</div>'
            '<table class="aud"><tr><th>channel</th>'
            '<th style="text-align:right">articles</th><th style="text-align:right">est. LLM $</th></tr>'
            f"{crows}</table>"
            '<div class="mut sm">Estimated pipeline (extract + classify + embed) cost to process each '
            "sourcing method's articles — what it costs to run rss / apify / the per-locale floor / "
            "cc-news. Apify's own scraping credits are the actual figure above, billed separately.</div>"
        )
    catcafe = (
        f'<a class="clink" href="{html.escape(CATCAFE_URL)}" title="per-call traces + token usage">'
        "open cat-cafe ↗</a>"
        if otlp
        else '<span class="mut">cat-cafe not wired</span>'
    )
    return (
        '<div class="ins">'
        ''
        f'<div class="mgrid">{cells}</div>'
        '<div class="bl mt">By stage</div>'
        '<table class="aud"><tr><th>stage</th><th>model</th>'
        '<th style="text-align:right">calls</th><th style="text-align:right">est. $</th></tr>'
        f"{srows}</table>"
        f"{channel_block}"
        '<div class="mut sm mt">LLM figures are <b>estimated</b> from cumulative call counts × '
        "per-model token estimates; the exact per-call cost (and live token usage) is in cat-cafe. "
        f"Apify is the actual billed figure from its usage API. Per-call detail: {catcafe}.</div>"
        f"{budget_line}"
        "</div>"
    )


def _acquisition_page(funnel, by_platform, referrers, daily, signups, ready: bool = True) -> str:
    """The maat.press funnel for the operator (pure): KPIs, platform split, referrers, the
    14-day trend, and the launch list. Degrades to a note when the projection isn't migrated."""
    back = '<div class="ins">'
    head = ''
    if not ready:
        return (
            f'{back}{head}<p class="empty">Not set up yet — restart the kernel (maat-kerneld) to '
            'apply the latest updates. Visitors to maat.press will show up here.</p></div>'
        )
    views = funnel.get("views", 0) or 0
    clicks = funnel.get("clicks", 0) or 0
    conv = f"{round(clicks / views * 100)}%" if views else "—"
    kpis = {
        "page views": views,
        "store clicks": clicks,
        "view → click": conv,
        "launch sign-ups": funnel.get("signups", 0) or 0,
        "beta testers": funnel.get("beta", 0) or 0,
    }
    kcells = "".join(
        f'<div class="mcell"><div class="mk">{html.escape(k)}</div><div class="mv">{v}</div></div>'
        for k, v in kpis.items()
    )
    plabel = {"ios": "iPhone · App Store", "mac": "Mac"}
    prows = "".join(
        f'<tr><td>{html.escape(plabel.get(r["platform"], r["platform"] or "unknown"))}</td>'
        f'<td class="mono">{r["clicks"]}</td></tr>'
        for r in by_platform
    ) or '<tr><td class="mut" colspan="2">No store clicks yet.</td></tr>'
    rrows = "".join(
        f'<tr><td>{html.escape(r["referrer"] or "direct")}</td><td class="mono">{r["clicks"]}</td></tr>'
        for r in referrers
    ) or '<tr><td class="mut" colspan="2">No referrers yet.</td></tr>'
    maxc = max((d.get("clicks", 0) for d in daily), default=0) or 1
    drows = "".join(
        f'<tr><td class="mut">{d["day"]:%b %d}</td><td class="mono">{d.get("views", 0)}</td>'
        f'<td class="mono">{d.get("clicks", 0)}</td>'
        f'<td><span style="display:block;height:7px;border-radius:5px;background:var(--acc);'
        f'min-width:2px;width:{round((d.get("clicks", 0) / maxc) * 100)}%"></span></td></tr>'
        for d in daily
    ) or '<tr><td class="mut" colspan="4">No activity in the last 14 days.</td></tr>'
    srows = "".join(
        f'<tr><td class="mono">{html.escape(s["email"])}</td>'
        f'<td>{html.escape(s.get("platform") or "—")}</td>'
        f'<td>{"✅" if s.get("beta") else "—"}</td>'
        f'<td class="mut">{s["first_seen"]:%Y-%m-%d %H:%M}</td>'
        f'<td class="mono">{s.get("hits", 1)}</td></tr>'
        for s in signups
    ) or '<tr><td class="mut" colspan="5">No sign-ups yet.</td></tr>'
    csv = (
        ' <a href="/acquisition/signups.csv" style="font-weight:600;color:var(--acc)">CSV ↓</a>'
        if signups else ""
    )
    return (
        f'{back}{head}'
        f'<div class="mgrid">{kcells}</div>'
        '<div class="bl mt">Store clicks by platform</div>'
        f'<table class="aud"><tr><th>platform</th><th>clicks</th></tr>{prows}</table>'
        '<div class="bl mt">Where clicks come from</div>'
        f'<table class="aud"><tr><th>referrer</th><th>clicks</th></tr>{rrows}</table>'
        '<div class="bl mt">Last 14 days</div>'
        '<table class="aud"><tr><th>day</th><th>views</th><th>clicks</th><th></th></tr>'
        f'{drows}</table>'
        f'<div class="bl mt">Launch list — asked to be told at launch{csv}</div>'
        '<table class="aud"><tr><th>email</th><th>platform</th><th>beta</th><th>first asked</th>'
        '<th>times</th></tr>'
        f'{srows}</table></div>'
    )


def wire_collapsed_sources(clusters, id_to_source: dict) -> set:
    """Sources collapsed as wire/cascade — present in a multi-article originator group (§5.5). Pure."""
    out: set = set()
    for cl in clusters:
        for grp in _jload(cl["originators"]):
            if len(grp) > 1:
                for art in grp:
                    out.add(id_to_source.get(art, art))
    return out


def _sources_page(srcs, wire: set, flag_by: dict, group_by: dict, registry: dict | None = None) -> str:
    registry = registry or {}
    note = (
        '<div class="deriv">Every outlet Maat has read. Its <b>lifecycle</b> runs registered → '
        'backfilling → scored → active; only <b>active</b> sources show in the feed (a new source '
        'is held until its articles corroborate and earn a reputation). <b>Deny</b> a source to drop '
        'stories sourced only from it; <b>Group</b> same-owner outlets so they count as one source in '
        'corroboration, not several.</div>'
    )
    rows = []
    for s in srcs:
        name = s["source"] or ""
        esc = html.escape(name)
        langs = ", ".join(x for x in (s["langs"] or []) if x) or "—"
        last = f'{s["last"]:%Y-%m-%d}' if s["last"] else "—"
        badges = []
        rec = registry.get(name)
        if rec is not None:
            if rec.state == sreg.ACTIVE:
                badges.append(_badge("active", "fact", "In the live feed"))
            else:
                badges.append(_badge(rec.state, "proj",
                                     "Held out of the feed until its backfill + scoring completes (#241)"))
        if is_primary_source(name):
            badges.append(_badge("first-hand", "fact",
                                 "A first-hand source, e.g. a body publishing its own statement"))
        if name in wire:
            badges.append(_badge("reprint", "proj",
                                 "Counted as a reprint of another outlet, not separate confirmation"))
        fl = flag_by.get(name) or {}
        if fl.get("status") == "deny":
            badges.append(_badge("denied", "laun", "You marked this source to be denied"))
        elif fl.get("status") == "allow":
            badges.append(_badge("allowed", "own", "You marked this source as allowed"))
        if group_by.get(name):
            badges.append(_badge(f"group · {group_by[name]}", "syn",
                                 "Grouped with same-owner outlets — they count as one source"))
        extra = ""
        if rec is not None:
            if rec.reputation is not None:
                extra += f' · reputation {rec.reputation:.2f}'
            if rec.provider:
                extra += f' · via {html.escape(rec.provider)}'
        rows.append(
            f'<div class="srow2"><div><div class="sname">{esc} '
            f'<span class="bs">{"".join(badges)}</span></div>'
            f'<div class="mut sm">{s["n"]} articles · {html.escape(langs)} · last {last}{extra}</div></div>'
            '<form class="inline" method="post" action="/sources/flag">'
            f'<input type="hidden" name="source" value="{esc}">'
            '<label class="toggle" title="Deny: drop feed stories sourced only from this outlet">'
            '<input type="checkbox" name="deny" value="1" onchange="this.form.submit()"'
            f'{" checked" if fl.get("status") == "deny" else ""}>'
            ' Deny</label></form>'
            '<form class="inline" method="post" action="/sources/group">'
            f'<input type="hidden" name="source" value="{esc}">'
            '<input name="group" placeholder="same-owner group">'
            '<button title="Outlets in one group count as a single source">Group</button></form></div>'
        )
    body = "".join(rows) or (
        '<p class="empty">No sources yet — pull some news first (the Updates tab).</p>'
    )
    return (
        '<div class="ins">'
        ''
        f"{note}{body}</div>"
    )


def _clocks_page(ing, daily, topics: list, paused: bool) -> str:
    """A1 — Updates page: the ingestion clock (live, pausable) + prediction-check (pending #39)."""
    last = f'{ing["last"]:%Y-%m-%d %H:%M}' if ing and ing["last"] else "never"
    n = (ing["n"] if ing else 0) or 0
    status = '<span class="b laun">Paused</span>' if paused else '<span class="b fact">On</span>'
    toggle_val = "false" if paused else "true"
    toggle_label = "Resume updates" if paused else "Pause updates"
    topics_html = ", ".join(html.escape(t) for t in topics) or (
        '<span class="mut">none set — add some in MAAT_TOPICS or config/topics.txt</span>'
    )
    ingest = (
        f'<div class="box"><div class="bl">News updates {status}</div>'
        f'<div class="mut sm" style="flex-basis:100%">Topics it follows: {topics_html}</div>'
        f'<div class="mut sm" style="flex-basis:100%">{n} articles pulled in so far · last {last}. '
        'Runs on a timer set outside the app; pausing here makes the next run skip.</div>'
        '<form class="inline" method="post" action="/clocks/set">'
        '<input type="hidden" name="clock" value="ingestion">'
        f'<input type="hidden" name="paused" value="{toggle_val}">'
        '<input class="reason" name="reason" placeholder="reason (optional)">'
        f'<button title="Pause: the next scheduled pull skips. Resume: it runs again.">'
        f'{toggle_label}</button></form></div>'
    )
    harvester = (
        '<div class="box"><div class="bl">Prediction check <span class="b own">coming soon</span></div>'
        '<div class="mut sm" style="flex-basis:100%">Will later check whether past predictions came '
        "true, and score forecasters on it. Not built yet (#39) — this turns on when it lands.</div></div>"
    )
    deltas = ""
    if daily:
        rows = "".join(f'<div class="kv"><b>{r["d"]:%Y-%m-%d}</b> {r["n"]} new</div>' for r in daily)
        deltas = f'<div class="bl mt">New articles per day (last 7 days)</div>{rows}'
    return (
        '<div class="ins">'
        ''
        f"{ingest}{harvester}{deltas}</div>"
    )


_GROUP_LABELS = {
    "Model routing": "AI models",
    "Extremity": "Extraordinary-claim bar",
    "Attribution": "Named vs anonymous sources",
    "Clustering": "Grouping & corroboration",
}


def _plain_group(g: str) -> str:
    """Plain-language section name for the Settings page — drops the §-refs. Pure."""
    base = g.split(" (§")[0].strip()
    return _GROUP_LABELS.get(base, base)


# How each prompt status is presented on the Prompts page.
_PROMPT_STATUS_BADGE = {  # status -> (badge text, badge css class, tooltip)
    "active": ("active", "fact", "Editable — saving makes it live on the next run"),
    "draft": ("draft", "proj", "A draft prompt — live like any other, but pending your review"),
    "on-device": ("on-device", "attr", "Runs on the reader's phone (Apple) — display-only mirror"),
}
# The "needs review" tag (#189): shown on a draft-seed prompt until the operator clears it. Purely
# informational — the prompt is already live; this never gates whether its path runs.
_NEEDS_REVIEW_BADGE = ("needs review", "floor", "Pending your review — click 'Mark reviewed' to clear")


def _prompt_review_control(key: str, needs_review: bool) -> str:
    """#189 — the inline 'Mark reviewed' button on a draft prompt that still carries the tag; nothing
    once it's been reviewed (or for active / on-device prompts, which never carry it). Pure."""
    if not needs_review:
        return ""
    k = html.escape(key)
    return (
        '<div class="bl mt"><form class="inline" method="post" action="/prompts/reviewed">'
        f'<input type="hidden" name="key" value="{k}">'
        '<span class="mut sm">This draft is live; it just hasn\'t been reviewed yet.</span> '
        '<button title="Clear the needs-review tag — does not change anything about what runs">'
        'Mark reviewed</button></form></div>'
    )


def _prompt_active_block(p: dict, by_key: dict, needs_review: bool = False) -> str:
    """An editable prompt's full editor — active text, version history, rollback, restore-default.
    Used for active AND draft prompts (drafts are live + editable like any other, #189); a draft that
    still needs review also shows its 'needs review' badge + 'Mark reviewed' button."""
    key = p["key"]
    versions = by_key.get(key, [])
    active = next((v for v in versions if v["active"]), None)
    text = active["text"] if active else p["default"]
    ver = f'version {active["version"]}' if active else "built-in (no edits yet)"
    badge, badge_cls, badge_tip = _PROMPT_STATUS_BADGE[p["status"]]
    review_badge = _badge(*_NEEDS_REVIEW_BADGE) if needs_review else ""
    review_ctl = _prompt_review_control(key, needs_review)
    must_keep = (
        f'<div class="mut sm">must keep: <span class="mono">{html.escape(" ".join(p["placeholders"]))}</span></div>'
        if p.get("placeholders") else ""
    )
    test_btn = (
        '<button formaction="/prompts/test" formnovalidate title="Run the golden tests with '
        'this text first — live AI calls, takes a moment">Test on goldens</button>'
        if key in prompts.GOLDEN_EVAL_KEYS else ""
    )
    past = [v for v in versions if not v["active"]]
    hist = ""
    if past:
        items = "".join(
            '<form class="vrow" method="post" action="/prompts/rollback">'
            f'<input type="hidden" name="key" value="{key}">'
            f'<input type="hidden" name="version" value="{v["version"]}">'
            f'<span class="vmeta mut sm">v{v["version"]} · {v["created_at"]:%Y-%m-%d %H:%M}'
            + (f' · {html.escape(v["reason"])}' if v["reason"] else "")
            + '</span><button class="ghost" title="Make this version active again">Roll back</button></form>'
            for v in past
        )
        hist = f'<div class="bl mt">Earlier versions</div>{items}'
    return (
        '<div class="box pedit">'
        f'<div class="cname">{html.escape(p["label"])} {_badge(badge, badge_cls, badge_tip)} '
        f'{review_badge} <span class="mut sm">— {html.escape(key)} · {ver}</span></div>'
        f'<div class="deriv">{html.escape(p.get("description", ""))}</div>'
        f'<div class="mut sm" title="Where the built-in text lives">source '
        f'<span class="mono">{html.escape(p["source"])}</span></div>'
        f'{review_ctl}'
        '<form method="post" action="/prompts/save">'
        f'<input type="hidden" name="key" value="{key}">'
        f'<textarea id="ta-{key}" class="prompt" name="text" rows="14">{html.escape(text)}</textarea>'
        f'{must_keep}'
        '<input class="reason" name="reason" placeholder="reason (optional, saved to History)">'
        '<div class="btnrow">'
        f'{test_btn}'
        '<button title="Saves a new version, live on the next run">Save new version</button>'
        '<button formaction="/prompts/restore" formnovalidate title="Replace with the original '
        'built-in version">Restore original</button>'
        '</div>'
        f'</form>{hist}</div>'
    )


def _prompt_chat_panel(key: str) -> str:
    """Always-open 'Improve with Claude' helper for the selected editable prompt (#158), shown in
    the right column. Raw-Claude chat: cauri discusses the prompt, a proposed revision drops into
    the editor on 'Apply to editor', and cauri still clicks Save new version. No auto-save."""
    k = html.escape(key)
    return (
        f'<div class="chat3" data-key="{k}">'
        '<div class="chat3-head">💬 Improve with Claude</div>'
        '<div class="chat-help mut sm">Talk through this prompt. When Claude proposes a revision '
        'you\'ll get an <b>Apply to editor</b> button — it fills the editor; you review and click '
        '<b>Save new version</b>. The chat never saves on its own.</div>'
        f'<div class="chat-log" id="log-{k}"></div>'
        '<div class="chat-row">'
        f'<textarea class="chat-in" id="in-{k}" rows="2" '
        'placeholder="e.g. make it stricter about attributed claims"></textarea>'
        f'<button type="button" class="chat-send" id="send-{k}" '
        f'onclick="maatPromptChat(\'{k}\')">Send</button>'
        '</div></div>'
    )


def _prompt_chat_unavailable() -> str:
    """Right-column placeholder for on-device prompts (Swift mirrors — read-only, no chat)."""
    return (
        '<div class="chat3"><div class="chat3-head">💬 Improve with Claude</div>'
        '<div class="chat-help mut sm">This prompt runs on the reader\'s phone (Apple) and is '
        'mirrored here for review only — edit it in the Apple app.</div></div>'
    )


def _prompt_readonly_block(p: dict) -> str:
    """An on-device prompt: label, status, source, and the full text — display-only (edited in the
    Apple app; Swift can't be imported, so this is a mirror)."""
    badge, badge_cls, badge_tip = _PROMPT_STATUS_BADGE["on-device"]
    return (
        '<div class="box" style="display:block">'
        f'<div class="cname">{html.escape(p["label"])} {_badge(badge, badge_cls, badge_tip)} '
        f'<span class="mut sm">— {html.escape(p["key"])}</span></div>'
        f'<div class="deriv">{html.escape(p.get("description", ""))}</div>'
        f'<div class="mut sm" title="Where the canonical text lives">source '
        f'<span class="mono">{html.escape(p["source"])}</span></div>'
        f'<textarea class="prompt" rows="14" readonly>{html.escape(p["default"])}</textarea>'
        '<div class="mut sm">Read-only — runs on the reader\'s phone; edit in the Apple app.</div>'
        '</div>'
    )


# Display order + short heading for each status group in the Prompts left-nav. One continuous list,
# ordered active → draft → on-device (#189): drafts are editable like any other, just tagged.
_PROMPT_GROUPS = [
    ("active", "Active"),
    ("draft", "Draft"),
    ("on-device", "On-device"),
]


def _prompt_nav(selected: str, review: dict[str, bool]) -> str:
    """Left column: every prompt as ONE selectable list, grouped by status; a draft still pending
    review carries the 'needs review' tag so the list shows what's left to look at (#189)."""
    out = []
    for status, short in _PROMPT_GROUPS:
        entries = [p for p in prompts.PROMPTS if p["status"] == status]
        if not entries:
            continue
        items = ""
        for p in entries:
            tag = f' {_badge(*_NEEDS_REVIEW_BADGE)}' if review.get(p["key"]) else ""
            items += (
                f'<a class="p3-item{" on" if p["key"] == selected else ""}" '
                f'href="/prompts?key={quote(p["key"])}" title="{html.escape(p.get("description", ""))}">'
                f'{html.escape(p["label"])}{tag}</a>'
            )
        out.append(f'<div class="p3-group">{html.escape(short)}</div>{items}')
    return "".join(out)


def _prompts_page(
    by_key: dict, store_ready: bool = True, selected: str = "",
    review: dict[str, bool] | None = None,
) -> str:
    """P8 — three panels: prompts (left, one list), the selected prompt's editor (middle), the
    always-open Claude chat (right). Active AND draft prompts are editable + versioned (drafts are
    live like any other, just tagged 'needs review' until cleared, #189); on-device is read-only."""
    review = review or {}
    store_note = ""
    if not store_ready:
        store_note = (
            '<div class="note">The prompt store isn\'t set up yet — restart the kernel '
            "(maat-kerneld) to enable saving and version history. Showing the built-in prompts.</div>"
        )
    p = prompts.PROMPTS_BY_KEY.get(selected)
    if p is None:  # no/invalid selection — default to the first editable agent
        p = next((x for x in prompts.PROMPTS if x["status"] == "active"),
                 prompts.PROMPTS[0] if prompts.PROMPTS else None)
        selected = p["key"] if p else ""
    if p is None:
        middle, right = '<p class="empty">No prompts registered.</p>', ""
    elif p["status"] == "on-device":  # Swift mirror — display-only, no chat
        middle, right = _prompt_readonly_block(p), _prompt_chat_unavailable()
    else:  # active OR draft — both editable; a draft pending review adds its tag + Mark-reviewed
        middle = _prompt_active_block(p, by_key, review.get(selected, False))
        right = _prompt_chat_panel(selected)
    return (
        '<div class="ins">'
        '<div class="deriv">Pick a prompt on the left to see and edit the exact instructions it runs '
        'on. Every prompt is live — saving makes the change take effect on the next run, every version '
        'is kept, and you can roll back; keep the <span class="mono">{placeholders}</span> or the save '
        'is refused. <b>Draft</b> prompts are editable just like the rest; they carry a '
        '<b>needs review</b> tag until you click <b>Mark reviewed</b> (informational only — it does '
        'not change what runs). <b>On-device</b> prompts run on the reader\'s phone (Apple) and are '
        'read-only. Use the chat on the right to shape a revision with Claude.</div>'
        f'{store_note}'
        '<div class="prompts3">'
        f'<aside class="p3-col p3-left">{_prompt_nav(selected, review)}</aside>'
        f'<section class="p3-col p3-mid">{middle}</section>'
        f'<aside class="p3-col p3-right">{right}</aside>'
        '</div></div>'
    )


def _replay_block(ab) -> str:
    """A/B-on-replay impact for a pending weight proposal (#123) — Brier before/after and how
    many resolved facts would change verdict. Shown at sign-off so approval is informed."""
    if ab is None:
        return ""
    if ab.n_scored == 0:
        return (
            '<div class="ovr" title="No facts have resolved yet, so the replay has nothing to '
            'score — the impact will appear once the loop accrues history.">A/B-on-replay: '
            'no resolved facts to test against yet</div>'
        )
    base = "—" if ab.brier_base is None else f"{ab.brier_base}"
    cand = "—" if ab.brier_candidate is None else f"{ab.brier_candidate}"
    better = (
        ab.brier_base is not None and ab.brier_candidate is not None
        and ab.brier_candidate < ab.brier_base
    )
    arrow = " ↓ better-calibrated" if better else ""
    return (
        '<div class="ovr" title="Replay-before-promote (D18): the candidate scored against every '
        'fact that has already resolved.">A/B-on-replay over <b>{n}</b> resolved facts · '
        'Brier <span class="mono">{b}→{c}</span>{arrow} · <b>{flips}</b> would change verdict '
        '({up} promoted, {down} demoted)</div>'.format(
            n=ab.n_scored, b=base, c=cand, arrow=arrow,
            flips=ab.flips, up=ab.promoted, down=ab.demoted,
        )
    )


def _knob_history_block(key: str, history: dict | None) -> str:
    """Per-knob change history (#123) — every proposal/revert filed, latest first."""
    rows = (history or {}).get(key) or []
    if not rows:
        return ""
    items = "".join(
        f'<span class="mut sm">{"reverted to" if h.get("reverted") else "proposed"} '
        f'<span class="mono">{html.escape(str(h.get("value", "")))}</span> · '
        f'{h["at"]:%Y-%m-%d %H:%M}'
        + (f' · {html.escape(str(h.get("actor")))}' if h.get("actor") else "")
        + '</span><br>'
        for h in rows[:6]
    )
    return f'<div class="mut sm" style="margin-top:4px">change history:<br>{items}</div>'


def _knob_input(k: dict) -> str:
    """The right input control for a knob, prefilled with its current live value: a model
    dropdown, an integer field, or a 0–1 number field."""
    cur = str(k["default"])
    t = k.get("type", "float")
    if t == "model":
        opts = list(k.get("options", []))
        if cur not in opts:
            opts = [cur, *opts]
        options = "".join(
            f'<option value="{html.escape(o)}"{" selected" if o == cur else ""}>{html.escape(o)}</option>'
            for o in opts
        )
        return f'<select name="value" aria-label="new value">{options}</select>'
    if t == "int":
        return f'<input type="number" name="value" step="1" min="1" value="{html.escape(cur)}">'
    return f'<input type="number" name="value" step="0.01" min="0" value="{html.escape(cur)}">'


def _config_page(
    overrides: dict, replay: dict | None = None, history: dict | None = None,
    active: dict | None = None,
) -> str:
    """F5 — render the knob registry grouped, with live defaults + pending proposals.

    For a pending weight proposal the A/B-on-replay impact (#123) is shown inline, plus a revert
    control and the per-knob change history."""
    out = [
        '<div class="ins">'
        ''
        '<div class="deriv">Changing a setting here records a <b>suggestion</b> (logged in History). '
        '<b>Applying suggestions to the live engine isn\'t wired up yet</b> — for now they\'re kept '
        'for review, and a scoring-weight change shows an <b>A/B-on-replay</b> preview (how it would '
        'have scored past facts). Settings that affect how truth is scored are marked '
        '<b>needs sign-off</b>: promoting one will need approval + an A/B pass once that step is built.</div>'
    ]
    for g in config.groups():
        out.append(f'<div class="bl mt">{html.escape(_plain_group(g))}</div>')
        for k in (kn for kn in config.KNOBS if kn["group"] == g):
            badge = (
                _badge("needs sign-off", "laun",
                       "A core scoring setting. Recorded as a suggestion only — promoting it to live "
                       "(with approval + an A/B-on-replay check) isn't wired up yet.")
                if k["core"]
                else _badge("minor", "own", "A lower-stakes setting")
            )
            ov = overrides.get(k["key"])
            live_val = (active or {}).get(k["key"])
            applied = False
            ov_html = ""
            if live_val is not None:  # #184: what the pipeline actually uses now
                ov_html += (
                    f'<div class="ovr">live → <b>{html.escape(str(live_val))}</b> '
                    '<span class="mut sm">promoted · the pipeline uses this</span></div>'
                )
            if ov:
                try:
                    applied = live_val is not None and float(ov["value"]) == float(live_val)
                except (TypeError, ValueError):
                    applied = live_val is not None and str(ov["value"]) == str(live_val)
                state = "applied (live)" if applied else "not applied yet"
                extra = f' · {html.escape(ov["reason"])}' if ov.get("reason") else ""
                ov_html += (
                    f'<div class="ovr">suggested → <b>{html.escape(ov["value"])}</b> '
                    f'<span class="mut sm">{ov["at"]:%Y-%m-%d %H:%M}{extra} · {state}</span></div>'
                )
                ov_html += _replay_block((replay or {}).get(k["key"]))
            ov_html += _knob_history_block(k["key"], history)
            revert = (
                '<form class="inline" method="post" action="/config/revert">'
                f'<input type="hidden" name="key" value="{html.escape(k["key"])}">'
                '<button title="Re-propose the built-in default for this setting (logged; still '
                'needs sign-off to go live)">Revert to default</button></form>'
            )
            promote = ""
            if ov and k["key"] in config._ENACTABLE and not applied:
                promote = (
                    '<form class="inline" method="post" action="/config/promote">'
                    f'<input type="hidden" name="key" value="{html.escape(k["key"])}">'
                    f'<input type="hidden" name="value" value="{html.escape(ov["value"])}">'
                    '<button title="Sign off: apply the suggested value to the live pipeline now.">'
                    'Promote to live</button></form>'
                )
            tip = (
                f'<span class="tip" data-tip="{html.escape(k["help"])}">i</span>'
                if k.get("help") else ""
            )
            out.append(
                '<div class="crow"><div class="cinfo">'
                f'<div class="cname">{html.escape(k["label"])}{tip} {badge}</div>'
                f'<div class="cur" title="Set in {html.escape(k["source"])}">now '
                f'<b>{html.escape(str(k["default"]))}</b></div>{ov_html}</div>'
                '<div class="cact">'
                '<form class="inline" method="post" action="/config/set">'
                f'<input type="hidden" name="key" value="{html.escape(k["key"])}">'
                f'{_knob_input(k)}'
                '<input class="reason" name="reason" placeholder="reason (optional)">'
                '<button title="Records your suggestion. Nothing changes live until reviewed.">'
                f'Suggest change</button></form>{promote}{revert}</div></div>'
            )
    out.append("</div>")
    return "".join(out)


def _reputation_tier(rep) -> tuple[str, str]:
    """Plain-language reliability standing from a SourceReputation record (§6.2). Sources with no
    resolved outcomes are 'not yet rated' (cold-start, §6.6) — never scored on consensus."""
    if rep.confirmation_rate is None:
        return "not yet rated", "own"
    r = rep.confirmation_rate
    if r >= 0.85:
        return "highly reliable", "fact"
    if r >= 0.6:
        return "generally reliable", "fact"
    if r >= 0.4:
        return "mixed reliability", "proj"
    return "unreliable", "laun"


def _reputation_page(reps, n_events: int) -> str:
    """A3 (#74) — render the reputation fold: per-source standing as a trajectory, not a snapshot.

    `reps` is a list of `learning.reputation.SourceReputation`. Each row surfaces the dimensions
    separately (cauri: never one magic number): independent-originator rate, attribution quality,
    solo-extraordinary red flags, and confirmation/refutation outcomes where the trajectory
    resolved them."""
    intro = (
        '<div class="deriv">A source\'s standing is measured against how its facts <b>actually '
        'resolved over time</b> (§6) — confirmed by independent corroboration or a primary source — '
        '<b>never</b> by agreeing with the crowd. This reads the real corroboration history; the '
        'Sources tab\'s reliability badges are a rougher live proxy. Each dimension is shown '
        'separately — no single score stands in for the others.</div>'
    )
    if not reps:
        body = (
            '<p class="empty">No reputation yet — it builds as the corroboration history grows '
            "(nothing has been corroborated to fold over).</p>"
        )
        return (
            '<div class="ins">'
            ''
            f"{intro}{body}</div>"
        )
    rows = []
    for rep in reps:
        tier, cls = _reputation_tier(rep)
        flags = [_badge(tier, cls, "Reliability standing — truthfulness over time, not consensus")]
        if rep.solo_extraordinary:
            flags.append(_badge(
                f"solo extraordinary ×{rep.solo_extraordinary}", "laun",
                "Stood alone on an extraordinary/significant claim — may be breaking it first, "
                "or fabricating. Surfaced as tension, not a verdict.",
            ))
        if rep.primary_appearances:
            flags.append(_badge(
                f"primary ×{rep.primary_appearances}", "fact",
                "Contributed a first-hand / primary-source signal this many times",
            ))
        conf_rate = "—" if rep.confirmation_rate is None else f"{round(rep.confirmation_rate * 100)}%"
        rows.append(
            f'<div class="srow2"><div><div class="sname">{html.escape(rep.source)} '
            f'<span class="bs">{"".join(flags)}</span></div>'
            f'<div class="mut sm">{rep.appearances} appearances · '
            f'{round(rep.independent_rate * 100)}% as an independent originator · '
            f'attribution quality {rep.mean_attribution_weight}</div></div>'
            f'<div class="rnum" title="Facts from this source that later confirmed vs were '
            f'refuted (where the trajectory resolved)"><b>{conf_rate}</b> confirmed'
            f'<div class="mut sm">{rep.facts_confirmed}✓ · {rep.facts_refuted}✗ · '
            f'{rep.facts_unresolved} in flight</div></div></div>'
        )
    return (
        '<div class="ins">'
        ''
        f"{intro}"
        f'<div class="mut sm">{len(reps)} sources folded over {n_events} corroboration events</div>'
        f'{"".join(rows)}</div>'
    )


def _dist_bars(dist: dict, *, limit: int = 8) -> str:
    """Horizontal share bars for a {key: fraction} distribution (de-US dashboard)."""
    items = list(dist.items())[:limit]
    if not items:
        return '<div class="mut sm">no data yet</div>'
    return "".join(
        f'<div class="dist"><span class="dk mono">{html.escape(str(k))}</span>'
        f'<span class="dbar"><span class="dfill" style="width:{round(v * 100)}%"></span></span>'
        f'<span class="dv mono">{round(v * 100)}%</span></div>'
        for k, v in items
    )


def _calibration_page(status, breakdown, geo_dist, lang_dist, health) -> str:
    """A4b (#76) — calibration + de-US-centering + pipeline-health dashboards over the live
    backends. `status` is a CalibrationStatus, `breakdown` a de_us.ScoreBreakdown, `health` a
    pipeline_health rollup. References the backends; recomputes nothing here."""
    # --- calibration (#60) ---
    if status.brier is None:
        calib = (
            '<div class="empty" style="padding:24px 0">Nothing has resolved yet — the calibration '
            'loop activates as the clock keeps acquiring and facts confirm or are refuted.</div>'
        )
    else:
        bins = "".join(
            f'<tr><td class="mono">[{b.lo:.2f},{b.hi:.2f})</td><td>{b.n}</td>'
            f'<td class="mono">{b.predicted}</td><td class="mono">{b.actual}</td>'
            f'<td class="mut sm">{_bin_flag(b)}</td></tr>'
            for b in status.bins
        )
        caveat = ""
        if status.refutation_bias:
            caveat = (
                '<div class="note" style="color:#8a2a1e">Caveat: every resolved fact confirmed — '
                'no refutations in view yet, so proposals skew optimistic. Treat as provisional '
                'until a refutation signal (retraction / contradiction) feeds the loop.</div>'
            )
        props = ""
        if status.proposals:
            pitems = "".join(
                f'<li><span class="mono">{html.escape(p["key"])} → {html.escape(p["value"])}</span> '
                f'<span class="mut sm">{html.escape(p["reason"])}</span></li>'
                for p in status.proposals
            )
            props = (
                '<div class="bl mt">Tune proposals <span class="b laun">needs sign-off</span></div>'
                '<div class="note">Filed to the Settings panel as suggestions — never auto-applied. '
                'Sign off there (with the A/B-on-replay impact) to promote.</div>'
                f'<ul class="prov">{pitems}</ul>'
            )
        else:
            props = '<div class="mut sm">No tune proposals — current weights already fit the history.</div>'
        calib = (
            f'<div class="evbanner ok">Brier {status.brier} · lower = better-calibrated</div>'
            f'<div class="mut sm">{status.n_scored} of {status.n_observations} facts resolved'
            + (f' · most-recent event {round((status.freshness_seconds or 0) / 3600, 1)}h ago'
               if status.freshness_seconds is not None else "")
            + '</div>'
            f'{caveat}'
            '<div class="bl mt">Reliability bins (predicted read vs fraction confirmed)</div>'
            '<table class="aud"><tr><th title="The confidence band, e.g. 60–70%">band</th><th title="How many resolved facts landed in this band">n</th><th title="The confidence Maat showed (what it predicted)">read</th><th title="Share that actually turned out true (observed)">confirmed</th>'
            f'<th></th></tr>{bins}</table>{props}'
        )
    # --- de-US-centering (#59) ---
    bd = breakdown
    axes = [
        ("Anglo share", bd.anglo, "US+UK share of sources stays below target"),
        ("Country spread", bd.concentration, "Low single-country concentration (HHI)"),
        ("Country diversity", bd.country_diversity, "Enough distinct originator countries"),
        ("Language diversity", bd.language_diversity, "Enough distinct languages"),
        ("Language balance", bd.language_dominance, "No single language dominates"),
    ]
    axis_html = "".join(
        f'<div class="mcell" title="{html.escape(tip)}"><div class="mk">{html.escape(name)}</div>'
        f'<div class="mv">{round(v * 100)}%</div></div>'
        for name, v, tip in axes
    )
    de_us_html = (
        f'<div class="evbanner {"ok" if bd.overall >= 0.6 else "bad"}">de-US-centering '
        f'{round(bd.overall * 100)}% · 1 = maximally diverse</div>'
        '<div class="mut sm">Measures whether the feed counters Anglo-American slant (§8). '
        'Country is guessed from the source domain; this never touches a confidence score.</div>'
        f'<div class="mgrid">{axis_html}</div>'
        '<div class="bl mt">Sources by country</div>'
        f'{_dist_bars(geo_dist)}'
        '<div class="bl mt">Content by language</div>'
        f'{_dist_bars(lang_dist)}'
    )
    # --- pipeline health (#61) ---
    status_cls = {"healthy": "fact", "degraded": "proj", "stalled": "laun", "empty": "own"}.get(
        health["status"], "own"
    )
    alerts = "".join(
        f'<li>{html.escape(a)}</li>' for a in health["alerts"]
    ) or '<li class="mut">none — pipeline looks healthy</li>'
    stage_rows = "".join(
        f'<tr><td>{html.escape(s["stage"])}</td><td>{s["count"]}</td>'
        f'<td class="mut sm">{html.escape(s["freshness"])}</td></tr>'
        for s in health["stages"]
    )
    health_html = (
        f'<div>Overall: {_badge(health["status"], status_cls)}</div>'
        '<div class="bl mt">Alerts</div>'
        f'<ul class="prov">{alerts}</ul>'
        '<div class="bl mt">Stages</div>'
        '<table class="aud"><tr><th>stage</th><th>done</th><th>freshness</th></tr>'
        f'{stage_rows}</table>'
        f'<div class="mut sm" style="margin-top:6px">{health["dead_letters"]["total"]} dead-letter '
        f'item(s) · {health["projections"]["clusters"]} clusters · '
        f'{health["projections"]["articles"]} articles</div>'
    )
    return (
        '<div class="ins">'
        ''
        f'{calib}</div>'
        '<div class="ins"><h3 class="ih">De-US-centering — is the feed countering Anglo slant?</h3>'
        f'{de_us_html}</div>'
        '<div class="ins"><h3 class="ih">Pipeline health — is everything running?</h3>'
        f'{health_html}</div>'
    )


def _bin_flag(b) -> str:
    """Calibration-bin direction flag (under/over-confident), matching format_status."""
    if b.predicted + 0.10 < b.actual:
        return "under-confident"
    if b.predicted > b.actual + 0.10:
        return "over-confident"
    return ""


_TRIAGE_LABELS = {
    "veracity-dispute": "veracity dispute",
    "source-quality": "source quality",
    "topic-request": "topic request",
    "bug": "bug",
    "ui": "display issue",
}


def _review_btn(item_id: str, action: str, label: str) -> str:
    return (
        '<form class="inline" method="post" action="/review/act">'
        f'<input type="hidden" name="item_id" value="{html.escape(str(item_id))}">'
        f'<input type="hidden" name="action" value="{action}">'
        f'<button title="{label} this item (logged as an operator decision; leaves the queue).">'
        f'{label}</button></form>'
    )


def _triage_row(it: dict, *, fresh: bool) -> str:
    """One feedback item row (works for both triaged events and live-classified previews)."""
    tri = it.get("triage") or it  # triaged events nest under 'triage'; previews are flat
    cat = tri.get("category", "")
    conf = tri.get("confidence", 0.0)
    auto = tri.get("auto_fixable") or tri.get("route") == "auto-fix"
    cls = "fact" if auto else "proj"
    cat_badge = _badge(_TRIAGE_LABELS.get(cat, cat), cls)
    fresh_badge = _badge("not yet triaged", "own", "Classified live just now (rules) — the batch "
                         "triage agent hasn't filed it yet") if fresh else ""
    when = it.get("submitted_at") or it.get("triaged_at")
    when_html = f'<span class="mut sm">{when:%Y-%m-%d %H:%M}</span>' if when else ""
    iid = it.get("item_id") or tri.get("item_id") or ""
    actions = (
        '<div class="cact">'
        f'{_review_btn(iid, "resolve", "Resolve")}{_review_btn(iid, "dismiss", "Dismiss")}'
        f'{_review_btn(iid, "fix", "Route to auto-fix")}</div>'
    ) if iid else ""
    return (
        f'<div class="mc"><div class="bs">{cat_badge}{fresh_badge} '
        f'<span class="mut sm">{html.escape(str(it.get("source", "")))} · '
        f'confidence {round(conf * 100)}% · {when_html}</span></div>'
        f'<div class="t">{html.escape(str(it.get("text", "")))}</div>'
        f'<div class="mut sm">{html.escape(str(tri.get("reason", "")))}</div>{actions}</div>'
    )


def _review_page(review, autofix, fresh, coordinated) -> str:
    """A5 (#77) — render the feedback triage queue. Review-routed items need an operator;
    auto-fix items are safe to PR. Coordinated bursts are flagged — feedback is untrusted input."""
    intro = (
        '<div class="deriv">User feedback, triaged. <b>Needs review</b> = a person must decide '
        '(veracity disputes, source-quality, anything ambiguous). <b>Safe to auto-fix</b> = a PR '
        'can be generated without sign-off (clear bugs, cosmetic UI). Feedback is <b>untrusted '
        'input</b>: a coordinated burst is a possible attack vector, surfaced below — not a '
        'mandate.</div>'
    )
    warn = ""
    if coordinated["suspicious"]:
        srcs = ", ".join(
            f"{html.escape(s)} ({n})" for s, n in coordinated["suspicious"].items()
        )
        warn = (
            '<div class="note" style="color:#8a2a1e">Possible coordinated feedback — a burst from: '
            f'{srcs}. Weigh these as a group, not as independent voices.</div>'
        )
    review_html = "".join(_triage_row(it, fresh=False) for it in review) or (
        '<div class="mut sm">Nothing in the review queue.</div>'
    )
    fresh_html = "".join(_triage_row(it, fresh=True) for it in fresh)
    autofix_html = "".join(_triage_row(it, fresh=False) for it in autofix) or (
        '<div class="mut sm">No auto-fixable items.</div>'
    )
    total = len(review) + len(fresh)
    return (
        '<div class="ins">'
        ''
        f"{intro}{warn}"
        f'<div class="bl mt">Needs review ({total})</div>{review_html}{fresh_html}'
        f'<div class="bl mt">Safe to auto-fix ({len(autofix)})</div>{autofix_html}</div>'
    )


# Capability grants (§5 bounded self-modification): which knobs an operator must approve vs which
# may auto-tune within a bounded envelope. The kernel grants these; the system can never
# self-escalate past them. Derived from the Config registry's `core` flag — a single source of truth.
def _capability_grants() -> list[dict]:
    grants = [
        {"name": "Confidence weights (decay, primary-lift, cap)", "auto": True,
         "note": "May be auto-tuned within a bounded envelope (Gamelan), but every change is a "
                 "sign-off-gated proposal with an A/B-on-replay justification — never applied live."},
        {"name": "Source preferences (soft ranking)", "auto": True,
         "note": "Adjusted from reputation within ±0.30 per step; a soft ordering signal only, "
                 "never used to suppress a fact."},
        {"name": "Scoring thresholds (gate floor, tiers)", "auto": False,
         "note": "Operator-gated. Changing how truth is scored needs explicit human sign-off."},
        {"name": "Judge / classifier model routing", "auto": False,
         "note": "Operator-gated. The model that judges veracity is never swapped automatically."},
        {"name": "Source allow / deny", "auto": False,
         "note": "Operator-gated. The system cannot grant or revoke a source's standing itself."},
        {"name": "Ownership grouping", "auto": False,
         "note": "Operator-provided. Co-owned outlets are collapsed only on an explicit edge."},
    ]
    return grants


def _policy_page(proposal, n_events: int) -> str:
    """A6 (#78) — render the bounded, sign-off-gated RL policy proposal + the capability grants.

    `proposal` is a `learning.rl.PolicyProposal` (always approved=False). The A/B-on-replay result
    justifies the weight side; source-preference changes stay within the safe envelope."""
    ab = proposal.ab
    gate = _badge("not applied — needs sign-off", "laun",
                  "Bounded self-modification (§5): the system may PROPOSE within a safe envelope; "
                  "only an operator can promote it.")
    intro = (
        '<div class="deriv">The learning loop proposes an improved policy — confidence weights '
        '(justified by an A/B-on-replay) and soft source preferences (from reputation). It is '
        f'<b>bounded</b> and <b>{gate}</b>: it can never apply itself or escalate past the grants '
        'below. Reviewed over the live corroboration history.</div>'
    )
    if ab.n_scored == 0:
        ab_html = (
            '<div class="mut sm">No facts have resolved yet — the weight side has nothing to '
            "replay against, so the proposed policy equals the current one.</div>"
        )
    else:
        better = (
            ab.brier_base is not None and ab.brier_candidate is not None
            and ab.brier_candidate < ab.brier_base
        )
        ab_html = (
            f'<div class="evbanner {"ok" if better else "bad"}">A/B-on-replay over '
            f'{ab.n_scored} resolved facts · Brier '
            f'<span class="mono">{ab.brier_base}→{ab.brier_candidate}</span> · '
            f'{ab.flips} change verdict ({ab.promoted} promoted, {ab.demoted} demoted)</div>'
        )
    wchanges = proposal.weight_changes
    if wchanges:
        witems = "".join(
            f'<li><span class="mono">{html.escape(c["key"])} → {html.escape(c["value"])}</span> '
            f'<span class="mut sm">{html.escape(c["reason"])}</span></li>'
            for c in wchanges
        )
        weights_html = (
            '<div class="bl mt">Proposed weight changes</div>'
            f'<ul class="prov">{witems}</ul>'
            '<form class="inline" method="post" action="/policy/approve">'
            '<input class="reason" name="reason" placeholder="reason (optional)">'
            '<button title="Sign off: promote these weight changes to the live pipeline now (same '
            'enact path as /config). Source-preference changes are not auto-applied.">'
            'Approve weight changes</button></form>'
        )
    else:
        weights_html = (
            '<div class="bl mt">Proposed weight changes</div>'
            '<div class="mut sm">None — current weights already fit the resolved history.</div>'
        )
    pchanges = proposal.pref_changes
    if pchanges:
        pitems = "".join(
            f'<li><span class="mono">{html.escape(c["source"])}: {c["before"]} → {c["after"]}</span> '
            f'<span class="mut sm">{html.escape(c["reason"])}</span></li>'
            for c in pchanges
        )
        prefs_html = (
            '<div class="bl mt">Proposed source-preference changes (within ±0.30 envelope)</div>'
            f'<ul class="prov">{pitems}</ul>'
        )
    else:
        prefs_html = (
            '<div class="bl mt">Proposed source-preference changes</div>'
            '<div class="mut sm">None within the safe envelope.</div>'
        )
    grows = "".join(
        f'<div class="crow"><div class="cinfo"><div class="cname">{html.escape(g["name"])} '
        + (_badge("auto-tunable (bounded)", "fact",
                  "May propose changes within a safe envelope — still sign-off-gated")
           if g["auto"] else
           _badge("operator-gated", "laun", "Only an operator can change this"))
        + f'</div><div class="mut sm">{html.escape(g["note"])}</div></div></div>'
        for g in _capability_grants()
    )
    return (
        '<div class="ins">'
        ''
        f"{intro}"
        f'<div class="mut sm">Proposed over {n_events} corroboration events · '
        f'{proposal.n_observations} resolved observations</div>'
        f'{ab_html}{weights_html}{prefs_html}</div>'
        '<div class="ins"><h3 class="ih">Capability grants — bounded self-modification (§5)</h3>'
        '<div class="deriv">What the system may adjust on its own (bounded, still sign-off-gated) '
        'versus what only an operator can change. The system can never grant itself more.</div>'
        f'{grows}</div>'
    )


def _eval_page(report, err: str, otlp: str) -> str:
    """A4a — render the eval harness output (#32). `report` is `maat.evals.evaluate(...)`."""
    status = (
        f'receiving traces at <span class="mono">{html.escape(otlp)}</span>'
        if otlp
        else 'not receiving yet — run <span class="mono">make obs-up</span> and set '
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    obs = (
        '<div class="deriv">Live traces &amp; AI-judge view: '
        f'<a class="clink" href="{html.escape(CATCAFE_URL)}" title="cat-cafe — per-call traces, '
        f'token cost, and the LLM judges you define">open cat-cafe ↗</a> '
        f'<span class="mut sm">· {status}</span></div>'
    )
    head = (
        '<div class="ins">'
        ''
        f'{obs}'
    )
    if report is None:
        return f'{head}<div class="empty">{html.escape(err)}</div></div>'
    m = report["metrics"]
    n_ok = sum(1 for s in report["stories"] if s.ok)
    banner = (
        f'<div class="evbanner {"ok" if report["passed"] else "bad"}">'
        f'{"PASS" if report["passed"] else "FAIL"} · {n_ok}/{len(report["stories"])} golden stories</div>'
    )
    kinds = ", ".join(f"{k}: {v}" for k, v in m["claim_kinds"].items()) or "—"
    labels = ", ".join(f"{k}: {v}" for k, v in m["labels"].items()) or "—"
    cells = [
        ("claims", f'{m["claims"]} <span class="mut">({html.escape(kinds)})</span>'),
        ("clusters",
         f'{m["clusters"]} <span class="mut">· primary {m["with_primary"]} '
         f'· extraordinary {m["extraordinary"]}</span>'),
        ("confidence mean", str(m["confidence_mean"])),
        ("labels", html.escape(labels)),
    ]
    mgrid = "".join(
        f'<div class="mcell"><div class="mk">{k}</div><div class="mv">{v}</div></div>' for k, v in cells
    )
    stories = []
    for s in report["stories"]:
        checks = "".join(
            f'<li>{"✓" if c.ok else "✗"} <b>{html.escape(c.field)}</b> '
            f'<span class="mono">{html.escape(c.detail)}</span></li>'
            for c in s.checks
        )
        stories.append(
            f'<div class="estory {"ok" if s.ok else "bad"}">'
            f'<div class="eh">{"✓" if s.ok else "✗"} {html.escape(s.name)}'
            f'<span class="mut"> — {html.escape((s.fact or "")[:70])}</span></div>'
            f'<ul class="prov">{checks or "<li class=mut>matched (no field checks)</li>"}</ul></div>'
        )
    return (
        f'{head}{banner}<div class="mgrid">{mgrid}</div>'
        f'<div class="bl mt">Test stories</div>{"".join(stories)}</div>'
    )


# ============================ chrome ========================================================


# Sidebar nav: (href, label, active-key, icon, purpose). The purpose doubles as the page's
# hover tooltip AND the context the right-panel assistant gets about the current page.
_NAV_TABS = [
    ("/", "Feed", "content", "📰", "The news feed — open any story to see or fix how Maat judged it"),
    ("/runs", "Activity", "runs", "📊", "What the system has processed, and anything that failed"),
    ("/review", "Review", "review", "💬", "User feedback, triaged — what needs a decision"),
    ("/clocks", "Updates", "clocks", "⏱️", "When Maat pulls in new news — and a switch to pause it"),
    ("/config", "Settings", "config", "⚙️", "The dials Maat runs on (changes are proposed, not auto-applied)"),
    ("/policy", "Policy", "policy", "🎚️", "What the learning loop would change — bounded, sign-off-gated"),
    ("/prompts", "Prompts", "prompts", "✏️", "Edit the instructions each AI step runs on (live on next run)"),
    ("/sources", "Sources", "sources", "📚", "Every outlet Maat reads, and how you want each one treated"),
    ("/reputation", "Reputation", "reputation", "⭐", "How each source has held up over time"),
    ("/calibration", "Calibration", "calibration", "🎯", "Is the confidence read right? Plus de-US-centering & health"),
    ("/eval", "Quality", "eval", "✅", "Automatic checks that Maat is still judging correctly"),
    ("/spend", "Spend", "spend", "💰", "What Maat has spent on AI + acquisition so far"),
    ("/acquisition", "Acquisition", "acquisition", "📈",
     "The maat.press marketing funnel — page views, App Store clicks, and the launch list"),
    ("/audit", "History", "audit", "🕘", "A log of every change made in this console"),
]
# active-key -> (label, purpose) — the assistant's page context.
_PAGE_META = {key: (label, tip) for _h, label, key, _i, tip in _NAV_TABS}


def _nav(active: str) -> str:
    links = [
        f'<a class="{"on" if key == active else ""}" href="{href}" data-tip="{html.escape(tip)}">'
        f'<span class="ico">{ico}</span>{label}</a>'
        for href, label, key, ico, tip in _NAV_TABS
    ]
    return f'<nav class="nav">{"".join(links)}</nav>'


def _assistant_panel(active: str) -> str:
    """The always-open right-panel assistant (page-aware Q&A). Hidden on Prompts, which has its
    own chat column. Carries the current page's label + purpose so the chat tells Claude where
    the operator is. The conversation is client-side; the reply comes from /assistant/chat."""
    if active == "prompts":
        return ""
    label, purpose = _PAGE_META.get(active, (active.title() or "Console", ""))
    return (
        f'<aside class="assistant" data-page="{html.escape(label)}" data-purpose="{html.escape(purpose)}">'
        '<div class="assistant-head">💬 Ask Claude <span>· about this page</span></div>'
        '<div class="chat-log" id="asst-log"></div>'
        '<div class="chat-row">'
        f'<textarea class="chat-in" id="asst-in" rows="2" placeholder="Ask about {html.escape(label)}…">'
        '</textarea>'
        '<button type="button" class="chat-send" id="asst-send" onclick="maatAssistant()">Send</button>'
        '</div></aside>'
    )


def _doc(main_html: str, subtitle: str, active: str, flash: str = "") -> str:
    banner = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    foot = (
        '<div class="sidebar-foot"><a href="/admin/logout" data-tip="End your admin session">'
        '<span class="ico">⏻</span>Sign out</a></div>'
        if _ADMIN.enabled else ""
    )
    title = _PAGE_META.get(active, (active.replace("-", " ").title() or "Console", ""))[0]
    return (
        _DOC.replace("{{nav}}", _nav(active))
        .replace("{{foot}}", foot)
        .replace("{{assistant}}", _assistant_panel(active))
        .replace("{{title}}", html.escape(title))
        .replace("{{subtitle}}", html.escape(subtitle))
        .replace("{{main}}", banner + main_html)
    )


def _redirect(path: str, msg: str = ""):
    """Redirect after a POST (PRG), carrying a one-line confirmation for the next page to show."""
    return RedirectResponse(f"{path}?ok={quote(msg)}" if msg else path, status_code=303)


_DOC = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maat console</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20fill='none'%3E%3Cpath%20d='M20%204C11%205%206%2010%205%2018c5%201%209-1%2012-5M9%2014c2-3%205-5%209-6M5%2018l-2%202'%20stroke='%23a8792e'%20stroke-width='1.8'%20stroke-linecap='round'%20stroke-linejoin='round'/%3E%3C/svg%3E">
<link rel="stylesheet" href="/static/console.css"></head><body>
<div class="app">
<aside class="sidebar">
<a class="brand" href="/"><svg class="brand-feather" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M20 4C11 5 6 10 5 18c5 1 9-1 12-5M9 14c2-3 5-5 9-6M5 18l-2 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg><b>Maat</b><span>operator console</span></a>
{{nav}}
{{foot}}
</aside>
<main>
<div class="topbar"><h1 class="ptitle">{{title}}</h1><div class="subtitle">{{subtitle}}</div></div>
{{main}}
</main>
{{assistant}}
</div>
<script>
// "Improve with chat" (#158): raw-Claude helper on each editable prompt. Per-key conversation,
// POST to /prompts/chat, render the reply; a fenced block becomes an "Apply to editor" button
// that fills THIS prompt's textarea. Never auto-saves — cauri reviews and clicks Save new version.
window.maatChats = window.maatChats || {};
function maatEsc(s){return String(s).replace(/[&<>"']/g,function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
// Split a reply into the prose around the FIRST fenced ``` block and the code inside it.
function maatSplit(text){
  var m=text.match(/```[^\n]*\n([\s\S]*?)```/);
  if(!m){return {html:maatEsc(text),code:null};}
  var before=text.slice(0,m.index), after=text.slice(m.index+m[0].length);
  var html=maatEsc(before.trim())+'<pre>'+maatEsc(m[1].replace(/\n$/,''))+'</pre>';
  if(after.trim()) html+='<div style="margin-top:6px">'+maatEsc(after.trim())+'</div>';
  return {html:html,code:m[1].replace(/\n$/,'')};
}
function maatBubble(log,cls,html){
  var d=document.createElement('div'); d.className='msg '+cls; d.innerHTML=html;
  log.appendChild(d); return d;
}
// --- chat plumbing shared by the streaming chats -------------------------------------------
// Read an NDJSON stream ({"t":delta} per token, {"error":msg}, {"done":true}); fire onDelta/onError.
function maatStream(url,body,onDelta,onError){
  return fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)}).then(async function(resp){
    if(!resp.ok||!resp.body){ onError('Chat unavailable — HTTP '+resp.status); return; }
    var reader=resp.body.getReader(), dec=new TextDecoder(), buf='', nl;
    while(true){
      var r=await reader.read(); if(r.done) break;
      buf+=dec.decode(r.value,{stream:true});
      while((nl=buf.indexOf('\n'))>=0){
        var line=buf.slice(0,nl).trim(); buf=buf.slice(nl+1);
        if(!line) continue;
        var m; try{ m=JSON.parse(line); }catch(_){ continue; }
        if(m.error){ onError(m.error); return; }
        if(m.t) onDelta(m.t);
      }
    }
  });
}
// Autoscroll only when the reader is already near the bottom (don't yank them off old messages).
function maatStick(log){ return log.scrollHeight-log.scrollTop-log.clientHeight<48; }
function maatScroll(log,stick){ if(stick) log.scrollTop=log.scrollHeight; }
function maatBusy(input,btn,busy,label){ input.disabled=busy; btn.disabled=busy; btn.textContent=busy?'…':label; }
function maatGrow(t){ if(!t) return; t.style.height='auto'; t.style.height=Math.min(t.scrollHeight,170)+'px'; }
function maatApplyBtn(editor,code){
  var b=document.createElement('button');
  b.type='button'; b.className='apply'; b.textContent='Apply to editor';
  b.title='Fill the prompt box above with this proposal — then review and Save new version';
  b.onclick=function(){ editor.value=code; editor.focus(); editor.scrollIntoView({block:'center'});
    b.textContent='Applied ✓'; b.disabled=true; };
  return b;
}

async function maatPromptChat(key){
  var input=document.getElementById('in-'+key), log=document.getElementById('log-'+key);
  var btn=document.getElementById('send-'+key), editor=document.getElementById('ta-'+key);
  if(!input||!log||!editor||input.disabled) return;
  var text=input.value.trim(); if(!text) return;
  var convo=window.maatChats[key]||(window.maatChats[key]=[]);
  convo.push({role:'user',content:text});
  maatBubble(log,'you',maatEsc(text));
  input.value=''; maatGrow(input);
  var label=btn.textContent; maatBusy(input,btn,true,label);
  var stick=maatStick(log); maatScroll(log,stick);
  var bubble=maatBubble(log,'bot streaming',''), acc='';
  try{
    await maatStream('/prompts/chat/stream',{key:key,current:editor.value,messages:convo},
      function(t){ acc+=t; bubble.textContent=acc; maatScroll(log,stick); },
      function(err){ bubble.className='msg err'; bubble.textContent=err; });
  }catch(e){ bubble.className='msg err'; bubble.textContent='Chat unavailable — '+((e&&e.message)||e); }
  if(bubble.classList.contains('streaming')){
    bubble.classList.remove('streaming');
    if(acc.trim()){
      convo.push({role:'assistant',content:acc});
      var parts=maatSplit(acc); bubble.innerHTML=parts.html;
      if(parts.code!==null) bubble.appendChild(maatApplyBtn(editor,parts.code));
    }else{ bubble.remove(); }
  }
  maatBusy(input,btn,false,label); input.focus(); maatScroll(log,stick);
}
// Enter sends, Shift+Enter = newline; textarea auto-grows as you type.
(function(){
  var ta=document.querySelector('.chat3 .chat-in'); if(!ta) return;
  var box=ta.closest('.chat3'); if(!box) return; var key=box.dataset.key;
  ta.addEventListener('keydown',function(e){
    if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); maatPromptChat(key); }});
  ta.addEventListener('input',function(){ maatGrow(ta); });
})();

// Page-aware console assistant (always-open right panel). Sends the current page + conversation
// to /assistant/chat; renders the reply. Read-only Q&A for now.
window.maatAsst = window.maatAsst || [];
async function maatAssistant(){
  var panel=document.querySelector('.assistant');
  var input=document.getElementById('asst-in');
  var log=document.getElementById('asst-log');
  var btn=document.getElementById('asst-send');
  if(!panel||!input||!log||!btn) return;
  var text=input.value.trim(); if(!text) return;
  window.maatAsst.push({role:'user',content:text});
  maatBubble(log,'you',maatEsc(text));
  input.value=''; btn.disabled=true; var label=btn.textContent; btn.textContent='…';
  log.scrollTop=log.scrollHeight;
  try{
    var resp=await fetch('/assistant/chat',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({page:panel.dataset.page,purpose:panel.dataset.purpose,messages:window.maatAsst})});
    var data=await resp.json();
    if(data.error){ maatBubble(log,'err',maatEsc(data.error)); }
    else{ window.maatAsst.push({role:'assistant',content:data.reply}); maatBubble(log,'bot',maatEsc(data.reply)); }
  }catch(e){ maatBubble(log,'err','Assistant unavailable — '+maatEsc(e.message||e)); }
  btn.disabled=false; btn.textContent=label; log.scrollTop=log.scrollHeight;
}
// Enter sends (Shift+Enter = newline).
(function(){var i=document.getElementById('asst-in');
  if(i)i.addEventListener('keydown',function(e){
    if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();maatAssistant();}});})();

// Activity — run the whole pipeline on one click, then poll progress and paint the per-step pills.
var maatRunTimer=null;
function maatPaintRun(st){
  var btn=document.getElementById('run-btn'), status=document.getElementById('run-status');
  (st.steps||[]).forEach(function(s,i){
    var el=document.getElementById('rs-'+i); if(!el) return;
    el.className='step-state '+(s.status||'idle');
    el.textContent={pending:'waiting',running:'running…',done:'done ✓',error:'failed'}[s.status]||'';
  });
  if(btn) btn.disabled=!!st.active;
  if(status){
    if(st.active) status.textContent='Running…';
    else if(st.error) status.textContent='Stopped: '+st.error;
    else if(st.finished) status.textContent='Done · finished '+String(st.finished).replace('T',' ');
    else status.textContent='';
  }
}
async function maatPollRun(){
  try{ var r=await fetch('/runs/status'); var st=await r.json(); maatPaintRun(st);
    if(st.active){ clearTimeout(maatRunTimer); maatRunTimer=setTimeout(maatPollRun,2000); }
  }catch(e){}
}
async function maatRunPipeline(){
  var btn=document.getElementById('run-btn'); if(!btn||btn.disabled) return;
  btn.disabled=true;
  try{ await fetch('/runs/run-all',{method:'POST'}); }catch(e){}
  maatPollRun();
}
(function(){ if(document.getElementById('run-btn')) maatPollRun(); })();  // reflect a run in progress
</script>
</body></html>"""
