"""Console v2 command/query API (#304) — one JSON contract between the Next.js console and the
event-sourced backend.

- **Queries** read the Postgres projections, reusing the existing serving/learning readers
  (`stories.py`, `reputation`, `obs_metrics`, `config`, `prompts`, `spend`, `feedback`).
- **Commands** emit typed ``ADMIN_*`` events via :func:`maat.events.publish` — the *only* way the
  app mutates state (D5/D28). The kernel is the single writer that folds them into projections, so
  audit, replay, and A/B-on-replay come for free.
- **`/events`** streams the live event log over SSE for the console's live indicator + Audit drawer.

Mounted at ``/console/api`` on the existing FastAPI app, exactly like ``feed_router`` — no new
backend, no business logic here. This is the contract Sia's tools (#306) and every room call; the
command table below doubles as Sia's tool manifest.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from maat import config, events
from maat import prompts as prompts_mod
from maat.clocks import is_paused
from maat.learning.reputation import (
    fold_reputation,
    reputation_score,
    reputation_trajectories,
)
from maat.learning import source_registry as sreg
from maat.obs import emit_consumer_health
from maat.obs_metrics import pipeline_health
from maat.serving import feedback as feedback_mod
from maat.serving import spend as spend_mod
from maat.serving.consumer_health import consumer_health, dead_letters_by_stage, health_as_dicts
from maat.serving.feed import story_to_json
from maat.serving.stories import load_story_detail, load_story_views

# FastAPI is imported at module scope but guarded so the pure helpers + command table stay importable
# in a stripped-down env (mirrors serving/feed.py). The router is None when FastAPI is absent.
try:  # pragma: no cover - import guard
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import StreamingResponse
except Exception:  # pragma: no cover
    APIRouter = HTTPException = Request = StreamingResponse = None  # type: ignore[assignment,misc]


# ─────────────────────────────────────────────────────────────────────────────────────────
# Pure helpers (no I/O — unit-tested without a DB or a bus)
# ─────────────────────────────────────────────────────────────────────────────────────────


def _jload(value: Any) -> Any:
    """asyncpg returns ``jsonb`` as ``str`` (no codec set); decode it, pass through dicts/lists."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None
    return value


def event_to_sse(envelope: dict[str, Any], *, now_ms: int | None = None) -> dict[str, Any]:
    """Shape a raw event envelope ``{stream_id, type, data, tenant_id}`` into the compact frame the
    console's SSE client expects (``{type, stream_id, actor, ts, data}``)."""
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    actor = data.get("actor") if isinstance(data, dict) else None
    return {
        "type": envelope.get("type", "event"),
        "stream_id": envelope.get("stream_id"),
        "actor": actor,
        "ts": now_ms if now_ms is not None else int(time.time() * 1000),
        "data": data,
    }


@dataclass(frozen=True)
class CommandSpec:
    """One operator command. ``build(body, actor, reason)`` validates the request and returns
    ``(stream_id, event_data)`` to publish; ``requires_signoff`` flags the veracity-core mutations
    that must be explicitly confirmed (D28). ``summary``/``fields`` document it for Sia + the UI."""

    event_type: str
    build: Callable[[dict[str, Any], str, str], tuple[str, dict[str, Any]]]
    summary: str
    fields: tuple[str, ...]
    requires_signoff: bool = False


def _need(body: dict[str, Any], key: str) -> Any:
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"missing required field: {key}")
    return value


def _b_claim_correct(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    claim_id = _need(body, "claim_id")
    fields = {k: body[k] for k in ("kind", "voice", "speaker") if body.get(k) is not None}
    if not fields:
        raise ValueError("provide at least one of: kind, voice, speaker")
    return claim_id, events.admin_event(claim_id, actor=actor, reason=reason, **fields)


def _b_claim_flag(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    claim_id = _need(body, "claim_id")
    return claim_id, events.admin_event(claim_id, actor=actor, reason=reason, abuse=_need(body, "abuse"))


def _b_cluster_split(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    cid = _need(body, "cluster_id")
    into = body.get("into") or []
    if not isinstance(into, list) or not into:
        raise ValueError("`into` must be a non-empty list of new cluster ids")
    return cid, events.admin_event(cid, actor=actor, reason=reason, into=into)


def _b_cluster_merge(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    ids = body.get("merged") or []
    if not isinstance(ids, list) or len(ids) < 2:
        raise ValueError("`merged` must list at least two cluster ids")
    new_id = body.get("new_id") or ids[0]
    return new_id, events.admin_event(body.get("new_id") or "", actor=actor, reason=reason, merged=ids)


def _b_claim_move(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    claim_id = _need(body, "claim_id")
    return claim_id, events.admin_event(
        claim_id, actor=actor, reason=reason,
        from_cluster=_need(body, "from_cluster"), to_cluster=_need(body, "to_cluster"),
    )


def _b_config_set(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    key = _need(body, "key")
    if key not in config.KNOBS_BY_KEY:
        raise ValueError(f"unknown config key: {key}")
    value = str(_need(body, "value"))
    return key, events.admin_event(key, actor=actor, reason=reason, key=key, value=value)


def _b_config_promote(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    key = _need(body, "key")
    if key not in config._ENACTABLE:
        raise ValueError(f"`{key}` isn't wired into the pipeline yet — can't promote")
    value = str(_need(body, "value"))
    return key, events.admin_event(key, actor=actor, reason=reason, key=key, value=value)


def _b_source_flag(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    source = _need(body, "source")
    status = _need(body, "status")
    if status not in ("allow", "deny"):
        raise ValueError("`status` must be 'allow' or 'deny'")
    return source, events.admin_event(source, actor=actor, reason=reason, source=source, status=status)


def _b_source_group(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    source = _need(body, "source")
    return source, events.admin_event(
        source, actor=actor, reason=reason, source=source, group=str(_need(body, "group"))
    )


def _b_clock_set(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    clock = _need(body, "clock")
    paused = bool(body.get("paused", False))
    return clock, events.admin_event(clock, actor=actor, reason=reason, clock=clock, paused=paused)


def _b_prompt_update(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    key = _need(body, "key")
    if key not in prompts_mod.EDITABLE_KEYS:
        raise ValueError(f"prompt `{key}` is not editable")
    text = _need(body, "text")
    missing = prompts_mod.missing_placeholders(key, text)
    if missing:
        raise ValueError(f"prompt is missing required placeholders: {', '.join(missing)}")
    return key, events.admin_event(key, actor=actor, reason=reason, key=key, text=text)


def _b_prompt_reviewed(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    key = _need(body, "key")
    return key, events.admin_event(key, actor=actor, reason=reason or "reviewed", key=key)


def _b_run_trigger(body: dict[str, Any], actor: str, reason: str) -> tuple[str, dict[str, Any]]:
    stage = body.get("stage") or "pipeline"
    return "pipeline", events.admin_event(
        "pipeline", actor=actor, reason=reason or "run from console", stage=stage
    )


COMMANDS: dict[str, CommandSpec] = {
    "claim.correct": CommandSpec(
        events.ADMIN_CLASSIFICATION_CORRECTED, _b_claim_correct,
        "Correct a claim's classification (kind/voice/speaker).", ("claim_id", "kind", "voice", "speaker"),
    ),
    "claim.flag_laundering": CommandSpec(
        events.ADMIN_LAUNDERING_FLAGGED, _b_claim_flag,
        "Flag a claim as laundering/abuse the classifier missed.", ("claim_id", "abuse"),
    ),
    "cluster.split": CommandSpec(
        events.ADMIN_CLUSTER_SPLIT, _b_cluster_split,
        "Split an over-merged cluster into new clusters.", ("cluster_id", "into"),
    ),
    "cluster.merge": CommandSpec(
        events.ADMIN_CLUSTER_MERGED, _b_cluster_merge,
        "Merge distinct clusters that are really one fact.", ("merged", "new_id"),
    ),
    "claim.move": CommandSpec(
        events.ADMIN_CLAIM_MOVED, _b_claim_move,
        "Move a claim from one cluster to another.", ("claim_id", "from_cluster", "to_cluster"),
    ),
    "config.set": CommandSpec(
        events.ADMIN_THRESHOLD_CHANGED, _b_config_set,
        "Propose a config knob change (recorded, not yet live).", ("key", "value"),
    ),
    "config.promote": CommandSpec(
        events.ADMIN_CONFIG_PROMOTED, _b_config_promote,
        "Promote a proposed knob into the live pipeline (sign-off).", ("key", "value"),
        requires_signoff=True,
    ),
    "source.flag": CommandSpec(
        events.ADMIN_SOURCE_FLAGGED, _b_source_flag,
        "Allow or deny a source.", ("source", "status"),
    ),
    "source.group": CommandSpec(
        events.ADMIN_SOURCE_GROUPED, _b_source_group,
        "Group a source under a shared owner/wire network.", ("source", "group"),
    ),
    "clock.set": CommandSpec(
        events.ADMIN_CLOCK_SET, _b_clock_set,
        "Pause or resume a pipeline clock.", ("clock", "paused"),
    ),
    "prompt.update": CommandSpec(
        events.ADMIN_PROMPT_UPDATED, _b_prompt_update,
        "Publish a new active version of an agent prompt (sign-off).", ("key", "text"),
        requires_signoff=True,
    ),
    "prompt.reviewed": CommandSpec(
        events.ADMIN_PROMPT_REVIEWED, _b_prompt_reviewed,
        "Mark a draft prompt as reviewed.", ("key",),
    ),
    "run.trigger": CommandSpec(
        events.ADMIN_RUN_TRIGGERED, _b_run_trigger,
        "Kick a pipeline run/stage.", ("stage",),
    ),
}


def command_manifest() -> list[dict[str, Any]]:
    """The command set as data — drives the API docs and Sia's tool list (#306)."""
    return [
        {
            "name": name,
            "event_type": spec.event_type,
            "summary": spec.summary,
            "fields": list(spec.fields),
            "requires_signoff": spec.requires_signoff,
        }
        for name, spec in COMMANDS.items()
    ]


# ─────────────────────────────────────────────────────────────────────────────────────────
# Router (thin: reads the pool / publishes to NATS; all logic is the reused readers above)
# ─────────────────────────────────────────────────────────────────────────────────────────


def _make_console_router() -> Any:
    router = APIRouter(prefix="/console/api", tags=["console"])

    def _pool(request: Request) -> Any:
        return request.app.state.pool

    def _actor(request: Request) -> str:
        admin = getattr(request.state, "admin", None)
        if isinstance(admin, dict) and admin.get("email"):
            return str(admin["email"])
        return "operator"

    # ---- identity ----------------------------------------------------------------------
    @router.get("/whoami")
    async def whoami(request: Request) -> dict[str, Any]:
        admin = getattr(request.state, "admin", None)
        if isinstance(admin, dict):
            return {"authenticated": True, "email": admin.get("email"), "sub": admin.get("sub")}
        return {"authenticated": False, "email": None, "sub": None}

    # ---- overview ----------------------------------------------------------------------
    @router.get("/overview")
    async def overview(request: Request) -> dict[str, Any]:
        pool = _pool(request)
        counts = {
            "articles": await pool.fetchval("select count(*) from articles") or 0,
            "claims": await pool.fetchval("select count(*) from claims") or 0,
            "clusters": await pool.fetchval("select count(*) from clusters") or 0,
            "events": await pool.fetchval("select count(*) from events") or 0,
        }
        clock_rows = await pool.fetch(
            "select data from events where type = $1 order by id desc limit 50", events.ADMIN_CLOCK_SET
        )
        clock_events = [_jload(r["data"]) for r in clock_rows]
        clocks = {
            c: is_paused(clock_events, c) for c in ("ingestion", "extraction", "corroboration")
        }
        dead_letters = await pool.fetchval("select count(*) from dead_letters") or 0
        last_ingest = await pool.fetchval(
            "select max(created_at) from events where type = 'article.ingested'"
        )
        return {
            "counts": counts,
            "clocks": clocks,
            "dead_letters": dead_letters,
            "last_ingest": last_ingest,
        }

    # ---- stories (product · credibility) -----------------------------------------------
    @router.get("/stories")
    async def stories(request: Request, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        views, total = await load_story_views(_pool(request))
        lim = max(1, min(limit, 200))
        off = max(0, offset)
        return {
            "total": total, "limit": lim, "offset": off,
            "stories": [story_to_json(v) for v in views[off:off + lim]],
        }

    @router.get("/stories/{node_id}")
    async def story_detail(node_id: str, request: Request) -> dict[str, Any]:
        view = await load_story_detail(_pool(request), node_id)
        if view is None:
            raise HTTPException(status_code=404, detail="no such story")
        return story_to_json(view, full=True)

    # ---- sources (product · one canonical reliability number) --------------------------
    @router.get("/sources")
    async def sources(request: Request) -> dict[str, Any]:
        pool = _pool(request)
        rows = await pool.fetch(
            "select source, count(*) n, max(ingested_at) last, min(ingested_at) first "
            "from articles where source is not null group by source order by n desc"
        )
        corr_events = [
            _jload(x["data"])
            for x in await pool.fetch(
                "select data from events where type = 'cluster.corroborated' order by id"
            )
        ]
        rep_by = {r.source: r for r in fold_reputation(corr_events)}
        traj_by = reputation_trajectories(corr_events)  # {source: [score, …]} sparkline
        registry = sreg.fold_sources(
            _jload(x["data"])
            for x in await pool.fetch(
                "select data from events where type in ($1, $2) order by id",
                events.SOURCE_REGISTERED, events.SOURCE_STATE_CHANGED,
            )
        )
        flags = {
            r["s"]: r["st"]
            for r in await pool.fetch(
                "select distinct on (data->>'source') data->>'source' s, data->>'status' st "
                "from events where type = $1 order by data->>'source', id desc",
                events.ADMIN_SOURCE_FLAGGED,
            )
        }
        out = []
        for r in rows:
            name = r["source"]
            rep = rep_by.get(name)
            reg = registry.get(name)
            out.append({
                "source": name,
                "articles": r["n"],
                "first_seen": r["first"],
                "last_seen": r["last"],
                "reliability": round(reputation_score(rep), 4) if rep else None,
                "trajectory": [round(p, 4) for p in traj_by.get(name, [])],
                "state": reg.state if reg else "unregistered",
                "status": flags.get(name, "allow"),
            })
        return {"total": len(out), "sources": out}

    # ---- claims (engine · the claim inspector — NOT the reader 'feed') ------------------
    @router.get("/claims")
    async def claims(request: Request, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        pool = _pool(request)
        lim = max(1, min(limit, 200))
        off = max(0, offset)
        total = await pool.fetchval("select count(*) from claims") or 0
        rows = await pool.fetch(
            "select c.id, c.text, c.kind, c.voice, c.speaker, c.in_headline, c.created_at, "
            "a.source, a.title, a.url, a.language "
            "from claims c join articles a on a.id = c.article_id "
            "order by c.created_at desc limit $1 offset $2",
            lim, off,
        )
        return {"total": total, "limit": lim, "offset": off, "claims": [_claim_row(r) for r in rows]}

    @router.get("/claims/{claim_id}")
    async def claim_detail(claim_id: str, request: Request) -> dict[str, Any]:
        pool = _pool(request)
        row = await pool.fetchrow(
            "select c.id, c.text, c.kind, c.voice, c.speaker, c.in_headline, c.evidence_span, "
            "c.relay_chain, c.created_at, a.source, a.title, a.url, a.language "
            "from claims c join articles a on a.id = c.article_id where c.id = $1",
            claim_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="no such claim")
        out = _claim_row(row)
        out["evidence_span"] = row["evidence_span"]
        out["relay_chain"] = _jload(row["relay_chain"])
        cluster = await pool.fetchrow(
            "select id, fact, confidence, extremity, independent_originators "
            "from clusters where claim_ids @> $1::jsonb limit 1",
            json.dumps([claim_id]),
        )
        out["cluster"] = (
            {
                "id": cluster["id"], "fact": cluster["fact"], "confidence": cluster["confidence"],
                "extremity": cluster["extremity"], "independent_originators": cluster["independent_originators"],
            }
            if cluster else None
        )
        return out

    # ---- pipeline (engine · health & ops) ----------------------------------------------
    @router.get("/pipeline")
    async def pipeline(request: Request) -> dict[str, Any]:
        pool = _pool(request)
        event_rows = [
            {"type": r["type"], "created_at": r["created_at"]}
            for r in await pool.fetch("select type, created_at from events")
        ]
        dead_rows = [
            {"type": r["type"], "error": r["error"], "created_at": r["created_at"]}
            for r in await pool.fetch(
                "select type, error, created_at from dead_letters order by id desc limit 50"
            )
        ]
        counts = {
            "articles": await pool.fetchval("select count(*) from articles") or 0,
            "claims": await pool.fetchval("select count(*) from claims") or 0,
            "clusters": await pool.fetchval("select count(*) from clusters") or 0,
        }
        clusters = [
            {"confidence": r["confidence"], "extremity": r["extremity"]}
            for r in await pool.fetch("select confidence, extremity from clusters")
        ]
        # Per-stage durable-consumer health (#299): live lag/in-flight/redelivered from JetStream +
        # per-stage dead-letter count — so the operator sees WHERE the pipeline backs up.
        nc = getattr(request.app.state, "nats", None)
        health = health_as_dicts(await consumer_health(nc, await dead_letters_by_stage(pool)))
        emit_consumer_health(health)  # → cat-cafe (no-op without an OTLP endpoint)
        result = pipeline_health(event_rows, dead_rows, counts, clusters=clusters)
        result["consumers"] = health
        return result

    @router.post("/dead-letters/{dl_id}/replay")
    async def replay_dead_letter(dl_id: int, request: Request) -> dict[str, Any]:
        """Re-publish a dead-lettered event so its stage re-processes it (#299). Idempotent handlers
        (#297) make a replay safe; a still-poison event simply dead-letters again."""
        pool = _pool(request)
        row = await pool.fetchrow(
            "select stream_id, type, data, tenant_id from dead_letters where id = $1", dl_id
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"no dead-letter #{dl_id}")
        nc = getattr(request.app.state, "nats", None)
        if nc is None:
            raise HTTPException(status_code=503, detail="event bus unavailable — nothing was replayed")
        data = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"] or "{}")
        await events.publish(nc, row["type"], row["stream_id"] or "", data, row["tenant_id"] or "cauri")
        return {"replayed": dl_id, "type": row["type"], "stream_id": row["stream_id"]}

    # ---- config (engine · tuning, sign-off) --------------------------------------------
    @router.get("/config")
    async def config_view(request: Request) -> dict[str, Any]:
        pool = _pool(request)
        proposed_rows = await pool.fetch(
            "select distinct on (data->>'key') data->>'key' k, data->>'value' v, "
            "data->>'reason' r, created_at from events where type = $1 "
            "order by data->>'key', id desc",
            events.ADMIN_THRESHOLD_CHANGED,
        )
        proposed = {
            r["k"]: {"value": r["v"], "reason": r["r"], "at": r["created_at"]} for r in proposed_rows
        }
        promoted_rows = await pool.fetch(
            "select data from events where type = $1 order by id", events.ADMIN_CONFIG_PROMOTED
        )
        active = config.active_config(_jload(r["data"]) for r in promoted_rows)
        knobs = [
            {
                "key": k["key"], "group": k["group"], "default": k["default"],
                "enactable": k["key"] in config._ENACTABLE,
                "active": active.get(k["key"]),
                "proposed": proposed.get(k["key"]),
            }
            for k in config.KNOBS
        ]
        return {"groups": config.groups(), "knobs": knobs}

    # ---- prompts (engine · tuning) -----------------------------------------------------
    @router.get("/prompts")
    async def prompts(request: Request) -> dict[str, Any]:
        review = await prompts_mod.review_map(_pool(request))
        return {
            "prompts": [
                {
                    "key": p["key"], "label": p.get("label", p["key"]), "status": p["status"],
                    "editable": p["key"] in prompts_mod.EDITABLE_KEYS,
                    "golden": p["key"] in prompts_mod.GOLDEN_EVAL_KEYS,
                    "needs_review": review.get(p["key"], False),
                }
                for p in prompts_mod.PROMPTS
            ]
        }

    @router.get("/prompts/{key}")
    async def prompt_detail(key: str, request: Request) -> dict[str, Any]:
        if key not in prompts_mod.PROMPTS_BY_KEY:
            raise HTTPException(status_code=404, detail="no such prompt")
        text = await prompts_mod.active_text(_pool(request), key, prompts_mod.seed_default(key))
        return {
            "key": key,
            "editable": key in prompts_mod.EDITABLE_KEYS,
            "status": prompts_mod.PROMPTS_BY_KEY[key]["status"],
            "text": text,
            "default": prompts_mod.seed_default(key),
        }

    # ---- feedback (inputs · triage) ----------------------------------------------------
    @router.get("/feedback")
    async def feedback(request: Request) -> dict[str, Any]:
        return {"queue": await feedback_mod.queue(_pool(request))}

    @router.post("/feedback/{item_id}/triage")
    async def triage_feedback(item_id: str, request: Request) -> dict[str, Any]:
        """Operator-triages a feedback item — an audited ``feedback.triaged`` event (D5)."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        category = str(body.get("category", "")).strip()
        route = str(body.get("route", "")).strip()
        if not category or not route:
            raise HTTPException(status_code=400, detail="`category` and `route` are required")
        nc = getattr(request.app.state, "nats", None)
        if nc is None:
            raise HTTPException(status_code=503, detail="event bus unavailable — nothing was saved")
        await feedback_mod.record_triage(
            _pool(request), nc,
            item_id=item_id, category=category, route=route, reason=str(body.get("reason", "")),
        )
        return {"ok": True, "item_id": item_id, "category": category, "route": route}

    # ---- business (spend) --------------------------------------------------------------
    @router.get("/spend")
    async def spend(request: Request) -> dict[str, Any]:
        pool = _pool(request)
        counts = {
            r["type"]: r["n"]
            for r in await pool.fetch(
                "select type, count(*) n from events where type in "
                "('claims.extracted','claims.classified','cluster.corroborated') group by type"
            )
        }
        n_claims = await pool.fetchval("select count(*) from claims") or 0
        n_articles = await pool.fetchval("select count(*) from articles") or 0
        rows, llm_total = spend_mod.estimate_llm_spend(
            extract_calls=counts.get("claims.extracted", 0),
            classify_calls=counts.get("claims.classified", 0),
            extremity_calls=counts.get("cluster.corroborated", 0),
            embed_claims=n_claims,
        )
        by_provider = {
            (r["p"] or "untagged"): r["n"]
            for r in await pool.fetch(
                "select coalesce(nullif(data->>'provider',''),'untagged') p, count(*) n "
                "from events where type = 'article.ingested' group by 1"
            )
        }
        providers = spend_mod.spend_by_provider(
            by_provider, avg_claims_per_article=(n_claims / n_articles if n_articles else 0.0)
        )
        return {"llm": {"stages": rows, "total_usd": llm_total}, "providers": providers}

    # ---- acquisition (business · the marketing funnel, event-sourced) -------------------
    @router.get("/acquisition")
    async def acquisition(request: Request) -> dict[str, Any]:
        pool = _pool(request)
        counts = {
            r["kind"]: r["n"]
            for r in await pool.fetch("select kind, count(*) n from acquisition_signals group by kind")
        }
        funnel = {
            "views": counts.get("view", 0),
            "clicks": counts.get("click", 0),
            "notifies": counts.get("notify", 0),
            "signups": await pool.fetchval("select count(*) from acquisition_signups") or 0,
            "beta": await pool.fetchval("select count(*) from acquisition_signups where beta") or 0,
        }
        by_platform = [
            {"platform": r["platform"], "clicks": r["clicks"]}
            for r in await pool.fetch(
                "select platform, count(*) clicks from acquisition_signals "
                "where kind = 'click' group by platform order by clicks desc"
            )
        ]
        return {"funnel": funnel, "by_platform": by_platform}

    # ---- audit (cross-cutting · the global change log) ---------------------------------
    @router.get("/audit")
    async def audit(request: Request, limit: int = 50) -> dict[str, Any]:
        lim = max(1, min(limit, 200))
        rows = await _pool(request).fetch(
            "select type, stream_id, data, created_at from events where type like 'admin.%' "
            "order by id desc limit $1",
            lim,
        )
        return {
            "events": [
                {
                    "type": r["type"], "stream_id": r["stream_id"], "at": r["created_at"],
                    "actor": (_jload(r["data"]) or {}).get("actor"),
                    "reason": (_jload(r["data"]) or {}).get("reason"),
                }
                for r in rows
            ]
        }

    # ---- commands (the only path that mutates state) -----------------------------------
    @router.get("/commands")
    async def commands_manifest() -> dict[str, Any]:
        return {"commands": command_manifest()}

    @router.post("/commands/{name}")
    async def run_command(name: str, request: Request) -> dict[str, Any]:
        spec = COMMANDS.get(name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown command: {name}")
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be a JSON object")
        reason = str(body.get("reason", ""))
        try:
            stream_id, data = spec.build(body, _actor(request), reason)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        nc = getattr(request.app.state, "nats", None)
        if nc is None:
            raise HTTPException(status_code=503, detail="event bus unavailable — nothing was saved")
        await events.publish(nc, spec.event_type, stream_id, data)
        await nc.flush()
        return {
            "ok": True, "command": name, "event_type": spec.event_type, "stream_id": stream_id,
            "requires_signoff": spec.requires_signoff,
        }

    # ---- live event stream (SSE) -------------------------------------------------------
    @router.get("/events")
    async def events_stream(request: Request) -> Any:
        nc = getattr(request.app.state, "nats", None)
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=1000)
        sub = None
        if nc is not None:
            async def _on_msg(msg: Any) -> None:
                try:
                    queue.put_nowait(msg.data)
                except asyncio.QueueFull:
                    pass

            sub = await nc.subscribe(f"{events.SUBJECT_PREFIX}.>", cb=_on_msg)

        async def _gen() -> Any:
            yield ": connected\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        raw = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # keep-alive comment
                        continue
                    envelope = _jload(raw)
                    if not isinstance(envelope, dict):
                        continue
                    yield f"data: {json.dumps(event_to_sse(envelope))}\n\n"
            finally:
                if sub is not None:
                    await sub.unsubscribe()

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    return router


def _claim_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "text": row["text"],
        "kind": row["kind"],
        "voice": row["voice"],
        "speaker": row["speaker"],
        "in_headline": row["in_headline"],
        "created_at": row["created_at"],
        "source": row["source"],
        "title": row["title"],
        "url": row["url"],
        "language": row["language"],
    }


console_router = _make_console_router() if APIRouter is not None else None
