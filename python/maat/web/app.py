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
import html
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import asyncpg
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
from maat.learning.reputation import fold_reputation
from maat.learning.rl import policy_step
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
from maat.serving.feed import feed_router
from maat.serving.feedback import queue as feedback_queue
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

# Mount the served-feed APIRouter (serving/feed.py) so the Apple client reads /api/v2/feed and
# /api/v2/story/{id} — confidence labels + de-US re-ranking — off the same projections this
# console reads. The router is None only if FastAPI is unavailable at import (test env guard).
if feed_router is not None:
    app.include_router(feed_router)


# ============================ routes: content (feed + inspectors) ============================


@app.get("/", response_class=HTMLResponse)
async def feed(ok: str = "") -> str:
    pool = app.state.pool
    articles = await pool.fetch(
        "select id, title, source, language from articles order by ingested_at desc"
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
    return _feed_page(articles, by_article, clusters, id_to_source, flash=ok)


@app.get("/cluster/{cid}", response_class=HTMLResponse)
async def cluster_detail(cid: str, ok: str = "") -> str:
    pool = app.state.pool
    cl = await pool.fetchrow("select * from clusters where id = $1", cid)
    if cl is None:
        return _doc('<div class="ins"><a class="back" href="/">← feed</a>'
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
        return _doc('<div class="ins"><a class="back" href="/">← feed</a>'
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


@app.post("/runs/trigger")
async def trigger_run(stage: str = Form(...), reason: str = Form("")):
    # Record operator intent in the audit log. Execution stays a deliberate CLI/cron step until a
    # job runner with budget guardrails (D22) is wired — the console must not silently spend API $.
    pub = await _publish(
        events.ADMIN_RUN_TRIGGERED, "pipeline", events.admin_event("pipeline", reason=reason, stage=stage)
    )
    return _redirect("/runs", "Logged. Run it yourself with the command shown." if pub else _BUS_DOWN)


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
    return _doc(_config_page(overrides, replay, history), "config", "config", flash=ok)


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
    return _doc(_sources_page(srcs, wire, flag_by, group_by), "sources", "sources", flash=ok)


@app.post("/sources/flag")
async def source_flag(source: str = Form(...), status: str = Form(...), reason: str = Form("")):
    msg = ""
    if status in ("allow", "deny"):
        pub = await _publish(
            events.ADMIN_SOURCE_FLAGGED,
            source,
            events.admin_event(source, reason=reason, source=source, status=status),
        )
        msg = f"Saved — {source} marked {status}. Not enforced yet." if pub else _BUS_DOWN
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
        msg = f"Saved — {source} grouped as '{group.strip()}'. Not enforced yet." if pub else _BUS_DOWN
    return _redirect("/sources", msg)


@app.get("/prompts", response_class=HTMLResponse)
async def prompts_view(ok: str = "") -> str:
    """P8 — edit the agent prompts directly. Edits go live on the next run; versioned + rollback."""
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
    return _doc(_prompts_page(by_key, store_ready), "prompts", "prompts", flash=ok)


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


@app.post("/prompts/test")
async def prompts_test(key: str = Form(...), text: str = Form(...)):
    """Eval-on-change: run the golden corpus with this candidate text (other stages stay on their
    active prompts) and report pass/fail — before the operator relies on it. Live LLM calls."""
    if key not in prompts.EDITABLE_KEYS:  # only the editable backend prompts can be golden-tested
        return _redirect("/prompts", "Unknown prompt.")
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


# ============================ routes: P8 dashboards (read-only, over real backends) ==========


async def _corroboration_history(pool) -> list[dict]:
    """The `cluster.corroborated` event stream, oldest→newest — the input every learning fold
    (reputation, calibration, RL) reads. Same query `scripts/calibrate_prod.py` uses."""
    rows = await pool.fetch(
        "select data from events where type = 'cluster.corroborated' order by id"
    )
    return [_jobj(r["data"]) for r in rows]


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


@app.get("/policy", response_class=HTMLResponse)
async def policy_view(ok: str = "") -> str:
    """A6 (#78) — RL policy control + capability grants. `learning.rl.policy_step` proposes a
    bounded, sign-off-gated policy improvement (weights via A/B-on-replay + source preferences
    within the safe envelope); it is NEVER auto-applied. The capability grants below state which
    knobs an operator must approve vs which may auto-tune — bounded self-modification (§5)."""
    history = await _corroboration_history(app.state.pool)
    proposal = policy_step(history)
    return _doc(_policy_page(proposal, len(history)), "policy", "policy", flash=ok)


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
    # Cloud fallback for §4 translate-for-display. The client translates ON-DEVICE first
    # (Apple Translation framework); it only calls this when the on-device pair is unavailable.
    # Real impl routes through the Source/Effect seam (maat/providers/seam.py) — model-translate,
    # never score a translation. Stubbed echo until that route is wired, so the reader runs keyless.
    return JSONResponse(
        {
            "translated": req.text,
            "source": req.source,
            "target": req.target,
            "engine": "cloud-fallback-stub",
        }
    )


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
    from collections import defaultdict

    arts = await pool.fetch("select id, source, language from articles")
    clusters = await pool.fetch(
        "select fact, originators, claim_ids, confidence, has_primary, created_at "
        "from clusters order by created_at"
    )
    claims = await pool.fetch("select id, article_id from claims")
    art_source = {a["id"]: a["source"] for a in arts}
    art_lang = {a["id"]: a["language"] for a in arts}
    claim_art = {str(c["id"]): c["article_id"] for c in claims}

    confs: dict[str, list] = defaultdict(list)
    facts: dict[str, set] = defaultdict(set)
    langs: dict[str, set] = defaultdict(set)
    primary_part: dict[str, bool] = defaultdict(bool)

    for cl in clusters:
        srcs = _cluster_sources(cl, art_source, claim_art)
        conf = float(cl["confidence"] or 0.0)
        for s in srcs:
            confs[s].append(conf)
            facts[s].add(cl["fact"])
            if cl["has_primary"]:
                primary_part[s] = True
        for cid in _jload(cl["claim_ids"]):
            aid = claim_art.get(str(cid))
            s = art_source.get(aid) if aid else None
            if s and art_lang.get(aid):
                langs[s].add(art_lang[aid])

    ratings = []
    for s in sorted({a["source"] for a in arts if a["source"]}):
        cs = confs.get(s, [])
        is_primary = _is_primary_name(s) or primary_part.get(s, False)
        cold = not cs and not is_primary
        reputation = 0.9 if is_primary else (sum(cs) / len(cs) if cs else 0.5)
        reputation = max(0.0, min(1.0, reputation))
        ratings.append(
            {
                "name": s,
                "reputation": round(reputation, 3),
                "tier": _source_tier(reputation, cold),
                "is_primary": is_primary,
                "n_stories": len(facts.get(s, set())),
                "cold_start": cold,
                "trajectory": [round(c, 3) for c in cs[-8:]] or [round(reputation, 3)],
                "languages": sorted(langs.get(s, set())) or ["en"],
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
            "provisional": True,
            "note": "Pre-reputation-fold proxy (P3 #37 not built): reputation approximated from "
            "corroboration confidence + primary-source standing, not the §6 truthfulness trajectory.",
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


def _card(a, claims) -> str:
    rows = "".join(_claim(c) for c in claims) or '<div class="claim t muted">no claims</div>'
    return (
        f'<article class="card"><div class="src">{html.escape(a["source"] or "")}</div>'
        f'<h2>{html.escape(a["title"] or "")}</h2>'
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
            f'<span class="ol">{lbl}</span>{html.escape(", ".join(names))}</div>'
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


def _feed_page(articles, by_article, clusters, id_to_source, flash: str = "") -> str:
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
    cards = "".join(_card(a, by_article.get(a["id"], [])) for a in articles)
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
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
        f'<div class="kv"><b>From article</b> {html.escape(c["art_title"] or "")} '
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
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
            '<div class="ins"><a class="back" href="/">← back to feed</a>'
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">History — every change made in this console</h3>'
        '<table class="aud"><tr><th>when</th><th>what</th><th>item</th><th>who</th>'
        f'<th>why</th><th>details</th></tr>{"".join(trs)}</table></div>'
    )


_STAGES = [
    ("Find articles", "article.ingested", "make acquire QUERY=… N=12  ·  make ingest-corpus"),
    ("Pull out claims", "claims.extracted", "make agents"),
    ("Label claims", "claims.classified", "make agents"),
    ("Score corroboration", "cluster.corroborated", "make corroborate"),
]


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
    for s in stages:
        last = f'{s["last"]:%Y-%m-%d %H:%M}' if s["last"] else "never"
        srows.append(
            f'<div class="srow" title="event: {html.escape(s["type"])}"><div class="sname">'
            f'{html.escape(s["label"])}</div>'
            f'<div class="snum">{s["count"]}<span class="mut sm"> done · last {last}</span></div>'
            f'<div class="scmd mono">{html.escape(s["cmd"])}</div>'
            '<form class="inline" method="post" action="/runs/trigger">'
            f'<input type="hidden" name="stage" value="{html.escape(s["label"])}">'
            '<button title="Notes that you started this step. It does not run it — you run that '
            'yourself, to control cost.">Log a run</button></form></div>'
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
            '<table class="aud"><tr><th>when</th><th>step</th><th>item</th><th>error</th></tr>'
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Activity — what the system has done, and anything that failed</h3>'
        f'<div class="mgrid">{pcells}</div><div class="bl mt">Steps</div>{"".join(srows)}'
        f'{dead_html}<div class="bl mt">Recent activity</div>'
        f'<table class="aud"><tr><th>when</th><th>step</th><th>item</th></tr>{rrows}</table>{note}</div>'
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


def _sources_page(srcs, wire: set, flag_by: dict, group_by: dict) -> str:
    note = (
        '<div class="deriv">Every outlet Maat has read. Mark a source <b>allow</b> or <b>deny</b>, or '
        '<b>group</b> outlets owned by the same company so they count as one source, not several. '
        'These are saved as preferences — they don\'t change scoring yet.</div>'
    )
    rows = []
    for s in srcs:
        name = s["source"] or ""
        esc = html.escape(name)
        langs = ", ".join(x for x in (s["langs"] or []) if x) or "—"
        last = f'{s["last"]:%Y-%m-%d}' if s["last"] else "—"
        badges = []
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
        rows.append(
            f'<div class="srow2"><div><div class="sname">{esc} '
            f'<span class="bs">{"".join(badges)}</span></div>'
            f'<div class="mut sm">{s["n"]} articles · {html.escape(langs)} · last {last}</div></div>'
            '<form class="inline" method="post" action="/sources/flag">'
            f'<input type="hidden" name="source" value="{esc}">'
            '<select name="status"><option value="deny">deny</option>'
            '<option value="allow">allow</option></select>'
            '<button title="Saved as a preference — not enforced yet">Save</button></form>'
            '<form class="inline" method="post" action="/sources/group">'
            f'<input type="hidden" name="source" value="{esc}">'
            '<input name="group" placeholder="same-owner group">'
            '<button title="Outlets in one group count as a single source">Group</button></form></div>'
        )
    body = "".join(rows) or (
        '<p class="empty">No sources yet — pull some news first (the Updates tab).</p>'
    )
    return (
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Sources — every outlet Maat reads, and how you want each treated</h3>'
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Updates — when Maat pulls in new news</h3>'
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
    "draft": ("draft", "proj", "In code but gated off — surfaced for review, not live or editable"),
    "on-device": ("on-device", "attr", "Runs on the reader's phone (Apple) — display-only mirror"),
}


def _prompt_active_block(p: dict, by_key: dict) -> str:
    """An editable backend prompt: active text, version history, rollback, restore-default."""
    key = p["key"]
    versions = by_key.get(key, [])
    active = next((v for v in versions if v["active"]), None)
    text = active["text"] if active else p["default"]
    ver = f'version {active["version"]}' if active else "built-in (no edits yet)"
    badge, badge_cls, badge_tip = _PROMPT_STATUS_BADGE["active"]
    past = [v for v in versions if not v["active"]]
    hist = ""
    if past:
        items = "".join(
            '<form class="inline" method="post" action="/prompts/rollback">'
            f'<input type="hidden" name="key" value="{key}">'
            f'<input type="hidden" name="version" value="{v["version"]}">'
            f'<span class="mut sm">v{v["version"]} · {v["created_at"]:%Y-%m-%d %H:%M}'
            + (f' · {html.escape(v["reason"])}' if v["reason"] else "")
            + '</span><button title="Make this version active again">Roll back</button></form>'
            for v in past
        )
        hist = f'<div class="bl mt">Earlier versions</div>{items}'
    return (
        '<div class="box" style="display:block">'
        f'<div class="cname">{html.escape(p["label"])} {_badge(badge, badge_cls, badge_tip)} '
        f'<span class="mut sm">— {html.escape(key)} · {ver}</span></div>'
        f'<div class="deriv">{html.escape(p.get("description", ""))}</div>'
        f'<div class="mut sm" title="Where the built-in text lives">source '
        f'<span class="mono">{html.escape(p["source"])}</span></div>'
        '<form method="post" action="/prompts/save">'
        f'<input type="hidden" name="key" value="{key}">'
        f'<textarea class="prompt" name="text" rows="14">{html.escape(text)}</textarea>'
        f'<div class="mut sm">must keep: <span class="mono">{html.escape(" ".join(p["placeholders"]))}</span></div>'
        '<input class="reason" name="reason" placeholder="reason (optional, saved to History)">'
        '<button formaction="/prompts/test" formnovalidate title="Run the golden tests with '
        'this text first — live AI calls, takes a moment">Test on goldens</button> '
        '<button title="Saves a new version, live on the next run">Save new version</button> '
        '<button formaction="/prompts/restore" formnovalidate title="Replace with the original '
        'built-in version">Restore original</button>'
        f'</form>{hist}</div>'
    )


def _prompt_readonly_block(p: dict) -> str:
    """A draft or on-device prompt: label, status, source, and the full text — display-only."""
    badge, badge_cls, badge_tip = _PROMPT_STATUS_BADGE[p["status"]]
    return (
        '<div class="box" style="display:block">'
        f'<div class="cname">{html.escape(p["label"])} {_badge(badge, badge_cls, badge_tip)} '
        f'<span class="mut sm">— {html.escape(p["key"])}</span></div>'
        f'<div class="deriv">{html.escape(p.get("description", ""))}</div>'
        f'<div class="mut sm" title="Where the canonical text lives">source '
        f'<span class="mono">{html.escape(p["source"])}</span></div>'
        f'<textarea class="prompt" rows="14" readonly>{html.escape(p["default"])}</textarea>'
        '<div class="mut sm">Read-only — surfaced for review, not edited here.</div>'
        '</div>'
    )


# Display order + heading for each status group on the Prompts page.
_PROMPT_GROUPS = [
    ("active", "Active — editable, live on the next run"),
    ("draft", "Draft — pending cauri review"),
    ("on-device", "On-device (Apple)"),
]


def _prompts_page(by_key: dict, store_ready: bool = True) -> str:
    """P8 — every prompt the platform runs, grouped by status. Active prompts are editable
    (text, version history, rollback, restore-default); draft and on-device prompts are read-only.
    """
    intro = (
        '<div class="deriv">Every prompt the platform runs, so you can review them all. '
        '<b>Active</b> prompts are editable — saving makes it live on the next run (the built-in '
        'version is the fallback), every version is kept, and you can roll back in one click; keep '
        'the <span class="mono">{placeholders}</span> or the save is refused. <b>Draft</b> prompts '
        'are in code but gated off, awaiting your review. <b>On-device</b> prompts run on the '
        "reader's phone (Apple) and are mirrored here for review. Draft and on-device are read-only."
        '</div>'
    )
    if not store_ready:
        intro += (
            '<div class="note">The prompt store isn\'t set up yet — restart the kernel '
            "(maat-kerneld) to enable saving and version history. Showing the built-in prompts.</div>"
        )
    sections = []
    for status, heading in _PROMPT_GROUPS:
        entries = [p for p in prompts.PROMPTS if p["status"] == status]
        if not entries:
            continue
        if status == "active":
            blocks = "".join(_prompt_active_block(p, by_key) for p in entries)
        else:
            blocks = "".join(_prompt_readonly_block(p) for p in entries)
        sections.append(f'<div class="bl mt">{html.escape(heading)}</div>{blocks}')
    return (
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Prompts — the instructions each AI step runs on</h3>'
        f'{intro}{"".join(sections)}</div>'
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


def _config_page(overrides: dict, replay: dict | None = None, history: dict | None = None) -> str:
    """F5 — render the knob registry grouped, with live defaults + pending proposals.

    For a pending weight proposal the A/B-on-replay impact (#123) is shown inline, plus a revert
    control and the per-knob change history."""
    out = [
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Settings — the dials Maat runs on</h3>'
        '<div class="deriv">A change here is saved as a <b>suggestion</b> — it does NOT take effect '
        'until it\'s reviewed and turned on. Settings that affect how truth is scored are marked '
        '<b>needs sign-off</b>. For a scoring-weight change you\'ll see its <b>A/B-on-replay</b> '
        'impact (how it would have scored past facts) before you sign off.</div>'
    ]
    for g in config.groups():
        out.append(f'<div class="bl mt">{html.escape(_plain_group(g))}</div>')
        for k in (kn for kn in config.KNOBS if kn["group"] == g):
            badge = (
                _badge("needs sign-off", "laun",
                       "Affects how truth is scored — won't go live without explicit approval")
                if k["core"]
                else _badge("minor", "own", "A lower-stakes setting")
            )
            ov = overrides.get(k["key"])
            ov_html = ""
            if ov:
                extra = f' · {html.escape(ov["reason"])}' if ov.get("reason") else ""
                ov_html = (
                    f'<div class="ovr">suggested → <b>{html.escape(ov["value"])}</b> '
                    f'<span class="mut sm">{ov["at"]:%Y-%m-%d %H:%M}{extra} · not applied yet</span></div>'
                )
                ov_html += _replay_block((replay or {}).get(k["key"]))
            ov_html += _knob_history_block(k["key"], history)
            revert = (
                '<form class="inline" method="post" action="/config/revert">'
                f'<input type="hidden" name="key" value="{html.escape(k["key"])}">'
                '<button title="Re-propose the built-in default for this setting (logged; still '
                'needs sign-off to go live)">Revert to default</button></form>'
            )
            out.append(
                '<div class="crow"><div class="cinfo">'
                f'<div class="cname">{html.escape(k["label"])} {badge}</div>'
                f'<div class="mut sm" title="Set in {html.escape(k["source"])}">currently '
                f'<span class="mono">{html.escape(str(k["default"]))}</span></div>{ov_html}</div>'
                '<div class="cact">'
                '<form class="inline" method="post" action="/config/set">'
                f'<input type="hidden" name="key" value="{html.escape(k["key"])}">'
                '<input name="value" placeholder="new value">'
                '<input class="reason" name="reason" placeholder="reason (optional)">'
                '<button title="Records your suggestion. Nothing changes live until reviewed.">'
                f'Suggest change</button></form>{revert}</div></div>'
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
            '<div class="ins"><a class="back" href="/">← back to feed</a>'
            '<h3 class="ih">Reputation — how each source has held up over time</h3>'
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Reputation — how each source has held up over time</h3>'
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
            '<table class="aud"><tr><th>band</th><th>n</th><th>read</th><th>confirmed</th>'
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Calibration — is the confidence read actually right, over time?</h3>'
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
    return (
        f'<div class="mc"><div class="bs">{cat_badge}{fresh_badge} '
        f'<span class="mut sm">{html.escape(str(it.get("source", "")))} · '
        f'confidence {round(conf * 100)}% · {when_html}</span></div>'
        f'<div class="t">{html.escape(str(it.get("text", "")))}</div>'
        f'<div class="mut sm">{html.escape(str(tri.get("reason", "")))}</div></div>'
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Review queue — user feedback that needs a decision</h3>'
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
            '<div class="note">These file to Settings as suggestions — sign off there to apply.</div>'
            f'<ul class="prov">{witems}</ul>'
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Policy — what the learning loop would change (and what it may never touch)</h3>'
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
        '<div class="ins"><a class="back" href="/">← back to feed</a>'
        '<h3 class="ih">Quality — automatic checks that Maat is still judging correctly</h3>'
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


def _nav(active: str) -> str:
    tabs = [
        ("/", "Feed", "content", "The news feed — open any story to see or fix how Maat judged it"),
        ("/runs", "Activity", "runs", "What the system has processed, and anything that failed"),
        ("/review", "Review", "review", "User feedback, triaged — what needs a decision"),
        ("/clocks", "Updates", "clocks", "When Maat pulls in new news — and a switch to pause it"),
        ("/config", "Settings", "config", "The dials Maat runs on (changes are proposed, not auto-applied)"),
        ("/policy", "Policy", "policy", "What the learning loop would change — bounded, sign-off-gated"),
        ("/prompts", "Prompts", "prompts", "Edit the instructions each AI step runs on (live on next run)"),
        ("/sources", "Sources", "sources", "Every outlet Maat reads, and how you want each one treated"),
        ("/reputation", "Reputation", "reputation", "How each source has held up over time (§6)"),
        ("/calibration", "Calibration", "calibration", "Is the confidence read right? Plus de-US-centering & health"),
        ("/eval", "Quality", "eval", "Automatic checks that Maat is still judging correctly"),
        ("/audit", "History", "audit", "A log of every change made in this console"),
    ]
    links = [
        f'<a class="{"on" if key == active else ""}" href="{href}" title="{html.escape(tip)}">{label}</a>'
        for href, label, key, tip in tabs
    ]
    return f'<nav class="nav">{"".join(links)}</nav>'


def _doc(main_html: str, subtitle: str, active: str, flash: str = "") -> str:
    banner = f'<div class="flash">{html.escape(flash)}</div>' if flash else ""
    return (
        _DOC.replace("{{nav}}", _nav(active))
        .replace("{{subtitle}}", html.escape(subtitle))
        .replace("{{main}}", banner + main_html)
    )


def _redirect(path: str, msg: str = ""):
    """Redirect after a POST (PRG), carrying a one-line confirmation for the next page to show."""
    return RedirectResponse(f"{path}?ok={quote(msg)}" if msg else path, status_code=303)


_DOC = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maat console</title><style>
:root{--bg:#faf9f7;--card:#fff;--ink:#1c1b19;--mut:#7a7770;--line:#ece9e3;--acc:#175fa5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
a{color:inherit}
header.top{padding:24px 20px 6px;max-width:820px;margin:0 auto}
header.top h1{margin:0;font-size:24px;letter-spacing:-.02em}
header.top h1 a{text-decoration:none}
header.top p{margin:4px 0 0;color:var(--mut);font-size:13px}
.nav{display:flex;gap:16px;align-items:center;margin:10px 0 0;font-size:13px;font-weight:600}
.nav a{text-decoration:none;color:var(--mut);padding-bottom:3px;border-bottom:2px solid transparent}
.nav a.on{color:var(--ink);border-bottom-color:var(--ink)}
.nav .dim{color:#c3bfb6;cursor:not-allowed}
main{max-width:820px;margin:0 auto;padding:12px 20px 60px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:14px 0}
.panel h3,.ih{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
.story{padding:15px 0;border-top:1px solid var(--line)}
.story:first-of-type{border-top:0}
.cfact{font-weight:600;font-size:16px;letter-spacing:-.01em}
.clink{text-decoration:none}.clink:hover{text-decoration:underline;text-decoration-color:var(--line)}
.cmeta{font-size:14px;color:#3a3833;margin:2px 0 7px}
.conf{display:flex;align-items:center;gap:9px;margin:7px 0}
.cbar{flex:1;height:7px;background:var(--line);border-radius:5px;overflow:hidden}
.cfill{height:100%;border-radius:5px}
.cpct{font-weight:700;font-size:14px;font-variant-numeric:tabular-nums}
.label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;padding:1px 8px;border-radius:20px}
.conf.hi .cfill{background:#3b6d11}.conf.hi .cpct{color:#3b6d11}.conf.hi .label{background:#eaf3de;color:#3b6d11}
.conf.mid .cfill{background:#92580a}.conf.mid .cpct{color:#92580a}.conf.mid .label{background:#faeeda;color:#92580a}
.conf.lo .cfill{background:#b3402e}.conf.lo .cpct{color:#b3402e}.conf.lo .label{background:#fbe4df;color:#b3402e}
.conf.floor .cfill{background:#8a2a1e}.conf.floor .cpct{color:#8a2a1e}.conf.floor .label{background:#f7d9d3;color:#8a2a1e}
.ex{font-size:11px;font-weight:600;padding:1px 8px;border-radius:20px;background:#f0efe9;color:#67645d}
.ex.extraordinary{background:#fbe4df;color:#b3402e}
.origs{display:flex;flex-direction:column;gap:5px}
.orig{font-size:13px;padding:5px 11px;border-radius:9px}
.orig.wire{background:#faeeda}
.orig.indep{background:#eaf3de}
.ol{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin-right:8px}
.sup-wrap{margin-top:10px;padding-top:9px;border-top:1px dashed var(--line)}
.sup-head{font-size:10px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);margin-bottom:5px}
.sup{display:flex;gap:8px;align-items:baseline;padding:2px 0;font-size:13px}
.sup-pct{font-weight:700;font-variant-numeric:tabular-nums;min-width:34px}
.sup-pct.hi{color:#3b6d11}.sup-pct.mid{color:#92580a}.sup-pct.lo{color:#b3402e}.sup-pct.floor{color:#8a2a1e}
.sup-fact{color:#3a3833}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
 padding:18px 18px 12px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.card .src{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.card h2{margin:4px 0 12px;font-size:19px;line-height:1.3;letter-spacing:-.01em}
.claim{padding:9px 0;border-top:1px solid var(--line)}
.claim .t{margin-top:3px}
.bs{display:flex;flex-wrap:wrap;gap:5px}
.b{font-size:11px;font-weight:600;padding:2px 8px;border-radius:20px;line-height:1.6}
.own{background:#f0efe9;color:#67645d}
.attr{background:#e6f1fb;color:#175fa5}
.fact{background:#eaf3de;color:#3b6d11}
.proj{background:#faeeda;color:#92580a}
.syn{background:#eeedfe;color:#4a3fb0}
.head{background:#1c1b19;color:#fff}
.corr{background:#def0f6;color:#0b6b86}
.laun{background:#fbe4df;color:#8a2a1e}
.foot{margin-top:10px;padding-top:8px;border-top:1px solid var(--line);font-size:12px;color:var(--mut)}
.muted,.empty,.mut{color:var(--mut)}
.empty{text-align:center;padding:60px 0}
.ins{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:14px 0}
.back{display:inline-block;margin-bottom:10px;font-size:13px;color:var(--mut);text-decoration:none}
.deriv{font-size:13px;color:#3a3833;background:#f6f5f2;border-radius:9px;padding:8px 11px;margin:8px 0}
.box{border:1px solid var(--line);border-radius:11px;padding:12px 13px;margin:11px 0;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.box .bl{flex-basis:100%;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:700}
.bl.mt{margin-top:14px;display:block}
.box label{font-size:13px;display:flex;gap:5px;align-items:center}
.box input,.box select,.inline input,.inline select{font:inherit;font-size:13px;padding:4px 7px;border:1px solid var(--line);border-radius:7px;background:#fff}
.box .reason,.inline .reason{flex:1;min-width:120px}
button{font:inherit;font-size:13px;font-weight:600;padding:5px 12px;border:1px solid var(--ink);border-radius:7px;background:var(--ink);color:#fff;cursor:pointer}
.ck{flex-basis:100%;font-size:13px;gap:7px}
.mc{padding:10px 0;border-top:1px solid var(--line)}
.mc .src{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin-top:3px}
.inline{display:flex;gap:6px;align-items:center;margin-top:7px}
.kv{font-size:14px;margin:5px 0}.kv b{font-weight:600}
.prov{margin:6px 0;padding-left:18px;font-size:13px}.sm{font-size:12px;margin-top:6px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.aud{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
.aud th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);border-bottom:1px solid var(--line);padding:6px 8px}
.aud td{padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
.atype{font-weight:600;color:var(--acc)}
.evbanner{display:inline-block;font-weight:700;font-size:13px;padding:4px 12px;border-radius:8px;margin:4px 0 12px}
.evbanner.ok{background:#eaf3de;color:#3b6d11}.evbanner.bad{background:#fbe4df;color:#b3402e}
.mgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:8px 0}
.mcell{background:#f6f5f2;border-radius:9px;padding:9px 11px}
.mk{font-size:10px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);font-weight:700}
.mv{font-size:15px;margin-top:2px}
.estory{border:1px solid var(--line);border-radius:9px;padding:9px 11px;margin:7px 0}
.estory.ok{border-color:#cfe3b6;background:#f6faef}
.estory.bad{border-color:#e9b8ae;background:#fdf4f1}
.eh{font-weight:600;font-size:14px}
.srow{display:grid;grid-template-columns:1.1fr 1.3fr 1.8fr auto;gap:10px;align-items:center;padding:8px 0;border-top:1px solid var(--line);font-size:13px}
.sname{font-weight:600}
.scmd{color:var(--mut);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.crow{display:grid;grid-template-columns:1fr auto;gap:12px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.cname{font-weight:600;font-size:14px}
.ovr{font-size:13px;color:#0b6b86;margin-top:3px}
.srow2{display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.cact{display:flex;flex-direction:column;gap:5px;align-items:flex-end}
.rnum{text-align:right;font-size:14px;white-space:nowrap}
.dist{display:flex;align-items:center;gap:8px;margin:3px 0;font-size:13px}
.dk{min-width:70px}
.dbar{flex:1;height:7px;background:var(--line);border-radius:5px;overflow:hidden}
.dfill{display:block;height:100%;background:var(--acc);border-radius:5px}
.dv{min-width:38px;text-align:right}
.prompt{width:100%;min-height:230px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.5;padding:10px;border:1px solid var(--line);border-radius:8px;background:#fff;resize:vertical;margin-bottom:6px}
.note{flex-basis:100%;font-size:12px;color:var(--mut);line-height:1.5;margin:-2px 0 2px}
.flash{max-width:820px;margin:10px auto 0;padding:9px 14px;border-radius:var(--border-radius-md,8px);background:#eaf3de;color:#3b6d11;font-size:13px;font-weight:500;border:1px solid #cfe3b6}
[title]{cursor:help}
.nav a[title],button[title]{cursor:pointer}
</style></head><body>
<header class="top"><h1><a href="/">Maat</a> <span class="mut" style="font-size:13px;font-weight:400">operator console</span></h1>
{{nav}}<p>{{subtitle}}</p></header>
<main>{{main}}</main></body></html>"""
