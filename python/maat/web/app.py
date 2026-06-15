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

import html
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from maat import config, events
from maat.bus import connect as nats_connect
from maat.clocks import is_paused, read_topics
from maat.evals import evaluate, load_expectations
from maat.pipeline.corroborate import (
    ClaimRow,
    cluster_id,
    confidence_label as _confidence_label,
    confidence_read,
    corroborate_fixed,
    is_primary_source,
)

CATCAFE_URL = os.environ.get("CATCAFE_URL", "http://localhost:8800")
ROOT = Path(__file__).resolve().parents[3]  # repo root (for config/topics.txt)

DB = os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")


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


# ============================ routes: content (feed + inspectors) ============================


@app.get("/", response_class=HTMLResponse)
async def feed() -> str:
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
    return _feed_page(articles, by_article, clusters, id_to_source)


@app.get("/cluster/{cid}", response_class=HTMLResponse)
async def cluster_detail(cid: str) -> str:
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
    return _doc(_cluster_page(cl, members, id_to_source, others), "cluster", "content")


@app.get("/claim/{clid}", response_class=HTMLResponse)
async def claim_detail(clid: str) -> str:
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
    return _doc(_claim_page(c, prov), "claim", "content")


@app.get("/audit", response_class=HTMLResponse)
async def audit(limit: int = 200) -> str:
    rows = await app.state.pool.fetch(
        "select type, data, created_at from events where type like 'admin.%' "
        "order by id desc limit $1",
        limit,
    )
    return _doc(_audit_page(rows), "audit", "audit")


@app.get("/eval", response_class=HTMLResponse)
async def eval_view() -> str:
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
    return _doc(_eval_page(report, err, otlp), "eval", "eval")


@app.get("/runs", response_class=HTMLResponse)
async def runs() -> str:
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
    dead = await pool.fetch(
        "select type, stream_id, error, created_at from dead_letters order by id desc limit 25"
    )
    return _doc(_runs_page(stage_summary(counts), proj, recent, dead), "runs", "runs")


@app.post("/runs/trigger")
async def trigger_run(stage: str = Form(...), reason: str = Form("")):
    # Record operator intent in the audit log. Execution stays a deliberate CLI/cron step until a
    # job runner with budget guardrails (D22) is wired — the console must not silently spend API $.
    await _publish(
        events.ADMIN_RUN_TRIGGERED, "pipeline", events.admin_event("pipeline", reason=reason, stage=stage)
    )
    return RedirectResponse("/runs", status_code=303)


@app.get("/clocks", response_class=HTMLResponse)
async def clocks_view() -> str:
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
    return _doc(_clocks_page(ing, daily, read_topics(ROOT), paused), "clocks", "clocks")


@app.post("/clocks/set")
async def clock_set(clock: str = Form(...), paused: str = Form(...), reason: str = Form("")):
    # Ops control (acquisition cadence), not veracity core — genuinely applied: the next tick
    # reads this flag and skips. No sign-off gate.
    await _publish(
        events.ADMIN_CLOCK_SET,
        clock,
        events.admin_event(clock, reason=reason, clock=clock, paused=(paused == "true")),
    )
    return RedirectResponse("/clocks", status_code=303)


@app.get("/config", response_class=HTMLResponse)
async def config_view() -> str:
    """F5 — show every tunable knob with its live default + any proposed (pending) override."""
    rows = await app.state.pool.fetch(
        "select distinct on (data->>'key') data->>'key' k, data->>'value' v, "
        "data->>'reason' r, created_at from events where type = $1 "
        "order by data->>'key', id desc",
        events.ADMIN_THRESHOLD_CHANGED,
    )
    overrides = {r["k"]: {"value": r["v"], "reason": r["r"], "at": r["created_at"]} for r in rows}
    return _doc(_config_page(overrides), "config", "config")


@app.post("/config/set")
async def config_set(key: str = Form(...), value: str = Form(...), reason: str = Form("")):
    # A proposal only — recorded + audited, never auto-applied. Promotion to live (esp. core
    # knobs: gate floor, scoring, judge model) needs sign-off + A/B-on-replay (D18 / §5).
    if key in config.KNOBS_BY_KEY and value.strip():
        await _publish(
            events.ADMIN_THRESHOLD_CHANGED,
            key,
            events.admin_event(key, reason=reason, key=key, value=value.strip()),
        )
    return RedirectResponse("/config", status_code=303)


@app.get("/sources", response_class=HTMLResponse)
async def sources_view() -> str:
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
    return _doc(_sources_page(srcs, wire, flag_by, group_by), "sources", "sources")


@app.post("/sources/flag")
async def source_flag(source: str = Form(...), status: str = Form(...), reason: str = Form("")):
    if status in ("allow", "deny"):
        await _publish(
            events.ADMIN_SOURCE_FLAGGED,
            source,
            events.admin_event(source, reason=reason, source=source, status=status),
        )
    return RedirectResponse("/sources", status_code=303)


@app.post("/sources/group")
async def source_group(source: str = Form(...), group: str = Form(...), reason: str = Form("")):
    if group.strip():
        await _publish(
            events.ADMIN_SOURCE_GROUPED,
            source,
            events.admin_event(source, reason=reason, source=source, group=group.strip()),
        )
    return RedirectResponse("/sources", status_code=303)


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
        await _publish(
            events.ADMIN_CLASSIFICATION_CORRECTED,
            clid,
            events.admin_event(clid, reason=reason, **fields),
        )
    return RedirectResponse(f"/claim/{clid}", status_code=303)


@app.post("/claim/{clid}/flag")
async def flag_claim(clid: str, abuse: str = Form(...), reason: str = Form("")):
    await _publish(
        events.ADMIN_LAUNDERING_FLAGGED, clid, events.admin_event(clid, reason=reason, abuse=abuse)
    )
    return RedirectResponse(f"/claim/{clid}", status_code=303)


@app.post("/cluster/{cid}/split")
async def split_cluster(cid: str, claim_ids: list[str] = Form(default=[]), reason: str = Form("")):
    pool = app.state.pool
    cl = await pool.fetchrow("select claim_ids, extremity from clusters where id = $1", cid)
    if cl is None:
        return RedirectResponse("/", status_code=303)
    members = _jload(cl["claim_ids"])
    picked = set(claim_ids)
    selected = [m for m in members if m in picked]
    rest = [m for m in members if m not in picked]
    if not selected or not rest:  # a no-op split: leave the cluster intact
        return RedirectResponse(f"/cluster/{cid}", status_code=303)
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
    return RedirectResponse("/audit", status_code=303)


@app.post("/cluster/merge")
async def merge_clusters(cluster_ids: list[str] = Form(default=[]), reason: str = Form("")):
    pool = app.state.pool
    ids = [c for c in cluster_ids if c]
    if len(ids) < 2:
        return RedirectResponse("/", status_code=303)
    rows = await pool.fetch(
        "select id, claim_ids, extremity from clusters where id = any($1::text[])", ids
    )
    if len(rows) < 2:
        return RedirectResponse("/", status_code=303)
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
    return RedirectResponse("/audit", status_code=303)


@app.post("/cluster/{from_cid}/move")
async def move_claim(
    from_cid: str, claim_id: str = Form(...), to_cluster: str = Form(...), reason: str = Form("")
):
    pool = app.state.pool
    src = await pool.fetchrow("select claim_ids, extremity from clusters where id = $1", from_cid)
    dst = await pool.fetchrow("select claim_ids, extremity from clusters where id = $1", to_cluster)
    if src is None or dst is None:
        return RedirectResponse(f"/cluster/{from_cid}", status_code=303)
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
    return RedirectResponse("/audit", status_code=303)


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


@app.get("/api/feed")
async def api_feed() -> JSONResponse:
    pool = app.state.pool
    clusters = await pool.fetch(
        "select id, fact, sources, originators, independent_originators, has_primary, "
        "claim_ids, confidence, extremity from clusters "
        "order by confidence desc, independent_originators desc"
    )
    meta = await _article_meta(pool)
    claims_by_id = await _claims_by_id(pool)
    stories = [_story_json(c, claims_by_id, meta) for c in clusters]
    return JSONResponse(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(stories),
            "stories": stories,
        }
    )


@app.get("/api/story/{cluster_id}")
async def api_story(cluster_id: str, deeper: int = 0) -> JSONResponse:
    pool = app.state.pool
    row = await pool.fetchrow(
        "select id, fact, sources, originators, independent_originators, has_primary, "
        "claim_ids, confidence, extremity from clusters where id = $1",
        cluster_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="no such story")
    meta = await _article_meta(pool)
    claims_by_id = await _claims_by_id(pool)
    story = _story_json(row, claims_by_id, meta)
    # The full articles behind the story — this is what the reader actually reads.
    full = await _article_full_map(pool)
    story["articles"] = [
        _article_json(full[aid]) for aid in _cluster_article_ids(row, claims_by_id) if aid in full
    ]
    if deeper:
        # Tier-3 "go deeper" (§2.1, §11): the PCC / server middle tier expands provenance,
        # fetches-and-verifies primary sources, and runs cross-language corroboration. Stubbed
        # here as the per-claim provenance the deeper pass would assemble — the PCC developer
        # surface is verified at P6 and slots in behind this boundary.
        story["deeper"] = {
            "note": "Tier-3 expansion (server/PCC stub): primary-source fetch-and-verify and "
            "cross-language corroboration would run here.",
            "provenance": [
                {
                    "claim_id": c["id"],
                    "voice": c["voice"],
                    "speaker": c["speaker"],
                    "evidence_span": c["evidence_span"],
                    "source": c["source"],
                }
                for c in story["claims"]
            ],
        }
    return JSONResponse(story)


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


def _badge(text: str, cls: str) -> str:
    return f'<span class="b {cls}">{html.escape(text)}</span>'


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
        badges.append(_badge("headline", "head"))
    if c["voice"] == "attributed":
        badges.append(_badge(f"said · {c['speaker'] or '?'}", "attr"))
    else:
        badges.append(_badge("own voice", "own"))
    if c["kind"] == "fact":
        badges.append(_badge("fact", "fact"))
    elif c["kind"] == "projection":
        extra = f" · {c['horizon']}" if c["horizon"] else ""
        badges.append(_badge(f"projection{extra}", "proj"))
    if c["is_synthesis"]:
        badges.append(_badge("synthesis", "syn"))
    if _rget(c, "corrected"):
        badges.append(_badge("corrected", "corr"))
    if _rget(c, "laundering_flag"):
        badges.append(_badge(f"laundering · {c['laundering_flag']}", "laun"))
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


def _feed_page(articles, by_article, clusters, id_to_source) -> str:
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
        cards = '<p class="empty">No articles yet — start the agents and ingest a corpus.</p>'
    subtitle = f"{n_stories or len(articles)} stories · corroboration over spread · confidence on every claim"
    return _doc(panel + cards, subtitle, "content")


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
        '<div class="bl">Split — tick the claims to pull into a new cluster (the #20 fix surface)</div>'
        f'{checks}'
        '<input class="reason" name="reason" placeholder="why (recorded in the audit log)">'
        '<button>Split selected</button></form>'
    )
    merge = (
        '<form class="box" method="post" action="/cluster/merge">'
        f'<input type="hidden" name="cluster_ids" value="{html.escape(cid)}">'
        '<div class="bl">Merge with another cluster (one fact split across two)</div>'
        f'<select name="cluster_ids">{_opts(others)}</select>'
        '<input class="reason" name="reason" placeholder="why (recorded)">'
        '<button>Merge</button></form>'
        if others else ""
    )
    mrows = []
    for m in members:
        move = (
            f'<form class="inline" method="post" action="/cluster/{cid}/move">'
            f'<input type="hidden" name="claim_id" value="{m["id"]}">'
            f'<select name="to_cluster">{_opts(others)}</select>'
            '<input class="reason" name="reason" placeholder="why">'
            '<button>Move</button></form>'
            if others else ""
        )
        mrows.append(
            f'<div class="mc"><div class="bs">{_claim_badges(m)}</div>'
            f'<div class="t"><a class="clink" href="/claim/{m["id"]}">{html.escape(m["text"])}</a></div>'
            f'<div class="src">{html.escape(m["art_source"] or "")}</div>{move}</div>'
        )
    return (
        '<div class="ins"><a class="back" href="/">← feed</a>'
        f'{_headline(cl, id_to_source)}'
        f'<div class="deriv">Confidence derivation: {html.escape(deriv)}</div>'
        f'{split}{merge}'
        f'<div class="bl mt">Member claims ({len(members)})</div>{"".join(mrows)}</div>'
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
        f'<div class="kv"><b>article</b> {html.escape(c["art_title"] or "")} '
        f'<span class="mut">({html.escape(c["art_source"] or "")}, {html.escape(c["art_language"] or "?")})</span></div>'
        f'<div class="kv"><b>evidence span</b> {html.escape(c["evidence_span"] or "—")}</div>'
        f'<div class="kv"><b>relay chain</b> {relay_html}</div>'
    )
    correct = (
        f'<form class="box" method="post" action="/claim/{clid}/correct">'
        '<div class="bl">Correct classification (each fix is an audited event)</div>'
        '<label>kind <select name="kind"><option value="">— keep —</option>'
        '<option value="fact">fact</option><option value="projection">projection</option></select></label>'
        '<label>voice <select name="voice"><option value="">— keep —</option>'
        '<option value="own">own</option><option value="attributed">attributed</option></select></label>'
        '<label>speaker <input name="speaker" placeholder="(unchanged)"></label>'
        '<input class="reason" name="reason" placeholder="why (recorded)">'
        '<button>Apply correction</button></form>'
    )
    flag = (
        f'<form class="box" method="post" action="/claim/{clid}/flag">'
        '<div class="bl">Flag a laundering abuse (§5.2) — makes the outlet own the claim</div>'
        '<select name="abuse"><option value="endorsement">endorsement</option>'
        '<option value="bare_repetition">bare repetition as fact</option>'
        '<option value="selective_amplification">selective amplification</option></select>'
        '<input class="reason" name="reason" placeholder="why (recorded)">'
        '<button>Flag</button></form>'
    )
    provenance = (
        '<div class="bl mt">Provenance — the events behind this claim</div>'
        f'<ul class="prov">{prov_rows or "<li class=mut>none</li>"}</ul>'
        '<div class="mut sm">Model / prompt version / trace_id are not captured yet — that '
        'capture lands with the eval surfacing in #75 (A4a).</div>'
    )
    return (
        '<div class="ins"><a class="back" href="/">← feed</a>'
        f'<div class="cfact">{html.escape(c["text"])}</div>'
        f'<div class="bs">{_claim_badges(c)}</div>{meta}'
        f'{correct}{flag}{provenance}</div>'
    )


def _audit_page(rows) -> str:
    if not rows:
        return (
            '<div class="ins"><a class="back" href="/">← feed</a>'
            '<p class="empty">No operator actions yet.</p></div>'
        )
    trs = []
    for r in rows:
        d = _jobj(r["data"])
        extras = {k: v for k, v in d.items() if k not in ("target", "actor", "reason")}
        ex = ", ".join(f"{k}={v}" for k, v in extras.items())
        trs.append(
            "<tr>"
            f'<td class="mut">{r["created_at"]:%Y-%m-%d %H:%M}</td>'
            f'<td><span class="atype">{html.escape(r["type"].removeprefix("admin."))}</span></td>'
            f'<td class="mono">{html.escape(str(d.get("target", "")))}</td>'
            f'<td>{html.escape(str(d.get("actor", "")))}</td>'
            f'<td>{html.escape(str(d.get("reason", "")))}</td>'
            f'<td class="mono">{html.escape(ex)}</td></tr>'
        )
    return (
        '<div class="ins"><a class="back" href="/">← feed</a>'
        '<h3 class="ih">Audit — every operator action, straight off the event log</h3>'
        '<table class="aud"><tr><th>when</th><th>action</th><th>target</th><th>actor</th>'
        f'<th>reason</th><th>fields</th></tr>{"".join(trs)}</table></div>'
    )


_STAGES = [
    ("Acquire / ingest", "article.ingested", "make acquire QUERY=… N=12  ·  make ingest-corpus"),
    ("Extract", "claims.extracted", "make agents"),
    ("Classify", "claims.classified", "make agents"),
    ("Corroborate", "cluster.corroborated", "make corroborate"),
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


def _runs_page(stages, proj, recent, dead) -> str:
    pcells = "".join(
        f'<div class="mcell"><div class="mk">{html.escape(k)}</div><div class="mv">{v}</div></div>'
        for k, v in proj.items()
    )
    srows = []
    for s in stages:
        last = f'{s["last"]:%Y-%m-%d %H:%M}' if s["last"] else "—"
        srows.append(
            '<div class="srow"><div class="sname">'
            f'{html.escape(s["label"])}<span class="mut sm"> · {html.escape(s["type"])}</span></div>'
            f'<div class="snum">{s["count"]}<span class="mut sm"> events · last {last}</span></div>'
            f'<div class="scmd mono">{html.escape(s["cmd"])}</div>'
            '<form class="inline" method="post" action="/runs/trigger">'
            f'<input type="hidden" name="stage" value="{html.escape(s["label"])}">'
            '<button title="record intent in the audit log">log run</button></form></div>'
        )
    dead_html = ""
    if dead:
        drows = "".join(
            f'<tr><td class="mut">{r["created_at"]:%m-%d %H:%M}</td>'
            f'<td class="mono" style="color:#b3402e">{html.escape(r["type"])}</td>'
            f'<td class="mono">{html.escape(str(r["stream_id"] or ""))}</td>'
            f'<td class="mono">{html.escape((r["error"] or "")[:160])}</td></tr>'
            for r in dead
        )
        dead_html = (
            f'<div class="bl mt">Dead-letter — projection failures ({len(dead)})</div>'
            '<table class="aud"><tr><th>when</th><th>type</th><th>stream</th><th>error</th></tr>'
            f"{drows}</table>"
        )
    rrows = "".join(
        f'<tr><td class="mut">{r["created_at"]:%m-%d %H:%M}</td>'
        f'<td><span class="atype">{html.escape(r["type"])}</span></td>'
        f'<td class="mono">{html.escape(str(r["stream_id"] or ""))}</td></tr>'
        for r in recent
    )
    note = (
        '<div class="mut sm">Token spend + timing are traced per LLM call to cat-cafe (see Eval). '
        "Per-run cost against the budget (D22), and projection replay (rebuild from the log, a kernel "
        "feature), are not wired yet — flagged, not faked.</div>"
    )
    return (
        '<div class="ins"><a class="back" href="/">← feed</a>'
        '<h3 class="ih">Runs — pipeline activity off the event log (F4)</h3>'
        f'<div class="mgrid">{pcells}</div><div class="bl mt">Stages</div>{"".join(srows)}'
        f'{dead_html}<div class="bl mt">Recent events</div>'
        f'<table class="aud"><tr><th>when</th><th>type</th><th>stream</th></tr>{rrows}</table>{note}</div>'
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
        '<div class="deriv">Registry from ingested articles. Allow/deny + ownership grouping are '
        "recorded, audited <b>proposals</b> — enforcement (deny → acquisition; the ownership graph "
        "→ §5.5 originator-collapse) is the follow-up wiring, not faked here. The ownership graph is "
        "a learned first-class asset (§5); this is where the operator seeds and corrects it.</div>"
    )
    rows = []
    for s in srcs:
        name = s["source"] or ""
        esc = html.escape(name)
        langs = ", ".join(x for x in (s["langs"] or []) if x) or "—"
        last = f'{s["last"]:%Y-%m-%d}' if s["last"] else "—"
        badges = []
        if is_primary_source(name):
            badges.append(_badge("primary", "fact"))
        if name in wire:
            badges.append(_badge("wire-collapsed", "proj"))
        fl = flag_by.get(name) or {}
        if fl.get("status") == "deny":
            badges.append(_badge("denied", "laun"))
        elif fl.get("status") == "allow":
            badges.append(_badge("allowed", "own"))
        if group_by.get(name):
            badges.append(_badge(f"group · {group_by[name]}", "syn"))
        rows.append(
            f'<div class="srow2"><div><div class="sname">{esc} '
            f'<span class="bs">{"".join(badges)}</span></div>'
            f'<div class="mut sm">{s["n"]} articles · {html.escape(langs)} · last {last}</div></div>'
            '<form class="inline" method="post" action="/sources/flag">'
            f'<input type="hidden" name="source" value="{esc}">'
            '<select name="status"><option value="deny">deny</option>'
            '<option value="allow">allow</option></select><button>flag</button></form>'
            '<form class="inline" method="post" action="/sources/group">'
            f'<input type="hidden" name="source" value="{esc}">'
            '<input name="group" placeholder="owner / wire group"><button>group</button></form></div>'
        )
    body = "".join(rows) or '<p class="empty">No sources yet — run acquisition (make acquire QUERY=… N=12).</p>'
    return (
        '<div class="ins"><a class="back" href="/">← feed</a>'
        '<h3 class="ih">Sources — registry, allow/deny, ownership graph (A2)</h3>'
        f"{note}{body}</div>"
    )


def _clocks_page(ing, daily, topics: list, paused: bool) -> str:
    """A1 — the two clocks (§9): ingestion (live, pausable) + projection-harvester (pending #39)."""
    last = f'{ing["last"]:%Y-%m-%d %H:%M}' if ing and ing["last"] else "—"
    n = (ing["n"] if ing else 0) or 0
    status = '<span class="b laun">paused</span>' if paused else '<span class="b fact">running</span>'
    toggle_val = "false" if paused else "true"
    toggle_label = "Resume" if paused else "Pause"
    topics_html = ", ".join(html.escape(t) for t in topics) or (
        '<span class="mut">none — set MAAT_TOPICS or config/topics.txt</span>'
    )
    ingest = (
        f'<div class="box"><div class="bl">Ingestion clock {status}</div>'
        f'<div class="mut sm" style="flex-basis:100%">Topics: {topics_html}</div>'
        f'<div class="mut sm" style="flex-basis:100%">{n} articles ingested all-time · last {last} · '
        'cadence is external (cron / systemd); one tick = <span class="mono">make tick</span></div>'
        '<form class="inline" method="post" action="/clocks/set">'
        '<input type="hidden" name="clock" value="ingestion">'
        f'<input type="hidden" name="paused" value="{toggle_val}">'
        '<input class="reason" name="reason" placeholder="why">'
        f'<button>{toggle_label} ingestion</button></form></div>'
    )
    harvester = (
        '<div class="box"><div class="bl">Projection-harvester clock</div>'
        '<div class="mut sm" style="flex-basis:100%">Not built yet (#39, P3) — will wake on schedule '
        "to resolve / extend / decay matured projections into the accuracy ledger (§7). This control "
        "lights up when the harvester lands.</div></div>"
    )
    deltas = ""
    if daily:
        rows = "".join(f'<div class="kv"><b>{r["d"]:%Y-%m-%d}</b> {r["n"]} ingested</div>' for r in daily)
        deltas = f'<div class="bl mt">Ingestion per day (last 7)</div>{rows}'
    return (
        '<div class="ins"><a class="back" href="/">← feed</a>'
        '<h3 class="ih">Clocks — the two cadences (§9) (A1)</h3>'
        f"{ingest}{harvester}{deltas}</div>"
    )


def _config_page(overrides: dict) -> str:
    """F5 — render the knob registry grouped, with live defaults + pending proposals."""
    out = [
        '<div class="ins"><a class="back" href="/">← feed</a>'
        '<h3 class="ih">Config — model routing + veracity thresholds (F5)</h3>'
        '<div class="deriv">Changes are recorded as audited, versioned events — they are '
        '<b>not auto-applied</b>. Core knobs (gate floor, scoring, judge/classifier models) need '
        'explicit sign-off + an A/B-on-replay pass before going live (D18 / §5); that promotion '
        "path is not wired yet.</div>"
    ]
    for g in config.groups():
        out.append(f'<div class="bl mt">{html.escape(g)}</div>')
        for k in (kn for kn in config.KNOBS if kn["group"] == g):
            badge = _badge("core · sign-off", "laun") if k["core"] else _badge("ops", "own")
            ov = overrides.get(k["key"])
            ov_html = ""
            if ov:
                extra = f' · {html.escape(ov["reason"])}' if ov.get("reason") else ""
                ov_html = (
                    f'<div class="ovr">proposed → <b>{html.escape(ov["value"])}</b> '
                    f'<span class="mut sm">{ov["at"]:%Y-%m-%d %H:%M}{extra} · pending sign-off</span></div>'
                )
            out.append(
                '<div class="crow"><div class="cinfo">'
                f'<div class="cname">{html.escape(k["label"])} {badge}</div>'
                f'<div class="mut sm">default <span class="mono">{html.escape(str(k["default"]))}</span> '
                f'· <span class="mono">{html.escape(k["source"])}</span></div>{ov_html}</div>'
                '<form class="inline" method="post" action="/config/set">'
                f'<input type="hidden" name="key" value="{html.escape(k["key"])}">'
                '<input name="value" placeholder="new value">'
                '<input class="reason" name="reason" placeholder="why">'
                '<button>Propose</button></form></div>'
            )
    out.append("</div>")
    return "".join(out)


def _eval_page(report, err: str, otlp: str) -> str:
    """A4a — render the eval harness output (#32). `report` is `maat.evals.evaluate(...)`."""
    obs = (
        f'<div class="deriv">Tracing → cat-cafe · OTLP <span class="mono">{html.escape(otlp)}</span> · '
        f'<a class="clink" href="{html.escape(CATCAFE_URL)}">open trace UI ↗</a></div>'
        if otlp
        else '<div class="mut sm">OTLP tracing off — set OTEL_EXPORTER_OTLP_ENDPOINT and run '
        "<span class=\"mono\">make obs-up</span> to stream LLM spans to cat-cafe.</div>"
    )
    head = (
        '<div class="ins"><a class="back" href="/">← feed</a>'
        '<h3 class="ih">Eval — golden regression + metrics (surfacing #32, not rebuilt)</h3>'
    )
    if report is None:
        return f'{head}<div class="empty">{html.escape(err)}</div>{obs}</div>'
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
        f'<div class="bl mt">Golden stories</div>{"".join(stories)}{obs}</div>'
    )


# ============================ chrome ========================================================


def _nav(active: str) -> str:
    tabs = [
        ("/", "Content", "content"),
        ("/runs", "Runs", "runs"),
        ("/clocks", "Clocks", "clocks"),
        ("/config", "Config", "config"),
        ("/sources", "Sources", "sources"),
        ("/eval", "Eval", "eval"),
        ("/audit", "Audit", "audit"),
    ]
    dimmed: list = []
    links = [
        f'<a class="{"on" if key == active else ""}" href="{href}">{label}</a>'
        for href, label, key in tabs
    ]
    links += [f'<span class="dim" title="{t}">{lbl}</span>' for lbl, t in dimmed]
    return f'<nav class="nav">{"".join(links)}</nav>'


def _doc(main_html: str, subtitle: str, active: str) -> str:
    return (
        _DOC.replace("{{nav}}", _nav(active))
        .replace("{{subtitle}}", html.escape(subtitle))
        .replace("{{main}}", main_html)
    )


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
</style></head><body>
<header class="top"><h1><a href="/">Maat</a> <span class="mut" style="font-size:13px;font-weight:400">operator console</span></h1>
{{nav}}<p>{{subtitle}}</p></header>
<main>{{main}}</main></body></html>"""
