"""Serving layer for the Analyse surface (P14, #365): the public paste-a-URL veracity endpoint.

Wires ``pipeline.analyse`` to the live system: a version-cached corpus index (cluster facts +
embeddings, matched at the §5.4 bar; matches hydrated with member claims + bodies in one bounded
query), the truth-over-time reputation fold, operator source denials, and the live web search
(Apify primary, GDELT fallback — the same acquisition channels the feed trusts).

Endpoints (mounted under /api/v2 like the feed):
  POST /api/v2/analyse           {url} → Server-Sent Events: claims stream in as they resolve,
                                  ending in the full analysis. Strictly rate-limited per IP and
                                  bounded by a global concurrency gate (LLM work per request).
  GET  /api/v2/analyse/{id}      A completed analysis by id (in-process LRU → events log) — the
                                  shareable, cache-fast path.

Isolation (locked): analysed URLs NEVER enter the canonical store — no articles/claims/clusters
rows, no reputation effect. Each completed analysis is published as an ``analysis.completed``
event (tenant ``public``); the kernel appends it to the events log (unknown types fold nothing),
which doubles as the durable cache and the operator's audit trail.

Public payload discipline ("what, not how" — marketing-messaging): verdicts, extremity, and
scores only. Never the corroboration mechanism — no cluster ids, no source lists, no originator
counts, no live-search coverage numbers.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
import traceback
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import numpy as np

from maat import events as events_mod
from maat.acquire import apify, gdelt
from maat.acquire.fetch import fetch_article
from maat.acquire.source_gate import prefiltered_reject
from maat.learning.reputation import fold_reputation, reputation_score
from maat.learning.trajectory import load_trajectory
from maat.pipeline.analyse import (
    AnalyseError,
    ArticleAnalysis,
    ClaimReading,
    CorpusFact,
    LiveCandidate,
    analyse_article,
    match_claims,
)
from maat.pipeline.corroborate import ClaimRow
from maat.pipeline.identity import canonical_source
from maat.providers.seam import mistral_embed
from maat.serving.buildcache import VersionCache, data_version
from maat.serving.ratelimit import PerIpRateLimiter, client_ip
from maat.serving.source_flags import denied_sources

try:  # same guard as serving/feed.py — importable without FastAPI for pure-fn tests
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    APIRouter = Request = JSONResponse = StreamingResponse = BaseModel = Field = None  # type: ignore[assignment,misc]

SCOPE_LINE = (
    "Maat measures whether this article's factual claims hold up against independent reporting"
    " — not tone, bias, or what it leaves out."
)

# ── knobs (box .env; defaults are production-sane) ───────────────────────────────────────────────
_TTL_S = int(os.environ.get("MAAT_ANALYSE_TTL", "21600"))          # re-analyse after 6h
_CONCURRENCY = int(os.environ.get("MAAT_ANALYSE_CONCURRENCY", "2"))  # LLM analyses in flight
_LIVE = os.environ.get("MAAT_ANALYSE_LIVE", "1") not in ("0", "false", "no")
_MAX_SEARCHES = int(os.environ.get("MAAT_ANALYSE_MAX_SEARCHES", "10"))
_MAX_CANDIDATES = int(os.environ.get("MAAT_ANALYSE_MAX_CANDIDATES", "18"))
# An analysis costs real LLM + search work, so the per-IP budget is strict: a small burst, then
# one every five minutes. GETs of finished analyses are NOT limited (bounded, cached reads).
_LIMITER = PerIpRateLimiter(
    capacity=float(os.environ.get("MAAT_ANALYSE_RATE_BURST", "3")),
    refill_per_sec=float(os.environ.get("MAAT_ANALYSE_RATE_RPS", str(1 / 300))),
)
_SEM = asyncio.Semaphore(max(1, _CONCURRENCY))

_RESULTS_MAX = 256
_RESULTS: OrderedDict[str, tuple[float, dict]] = OrderedDict()  # id -> (monotonic ts, payload)


# ── url identity ─────────────────────────────────────────────────────────────────────────────────

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$)", re.I)


def normalise_url(url: str) -> str:
    """One canonical form per article URL, so re-pastes hit the cache: lowercase scheme/host,
    strip the fragment, default ports, tracking params, and a trailing slash."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower().rstrip(".")
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    query = urlencode(
        [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not _TRACKING_PARAMS.match(k)]
    )
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), host, path, "", query, ""))


def analysis_id(url: str) -> str:
    """Stable id for an analysis = hash of the normalised URL (mirrors the clock's article ids)."""
    return "an-" + hashlib.sha1(normalise_url(url).encode()).hexdigest()[:18]


# ── SSRF guard (mirrors the image proxy's, serving/feed.py) ─────────────────────────────────────


async def _host_is_public(host: str, port: int) -> bool:
    """True only if EVERY resolved address for ``host`` is a public, routable IP. Blocks loopback,
    RFC-1918, link-local (incl. cloud metadata), reserved/multicast/unspecified. Residual gap: a
    redirect/DNS-rebind after this check — same accepted posture as the image proxy (#1)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for *_rest, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return False
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False
    return True


async def check_url(url: str) -> str | None:
    """None if the URL is safe to fetch from the box; else a user-facing error message."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return "only http(s) article URLs are supported"
    if not p.hostname:
        return "that doesn't look like a valid URL"
    if p.port not in (None, 80, 443):
        return "non-standard ports are not supported"
    if not await _host_is_public(p.hostname, p.port or (80 if p.scheme == "http" else 443)):
        return "that host is not reachable from Maat"
    return None


# ── corpus index (version-cached; hydration on match only) ──────────────────────────────────────


@dataclass(frozen=True)
class _Assets:
    facts: list[CorpusFact]                 # light — no members/bodies until hydrated
    claim_ids: dict[str, list[str]]         # cluster_id -> member claim ids
    embeds: np.ndarray | None               # rows aligned with ``facts`` (None → corpus leg off)
    reputation: dict[str, float]            # rated sources only (truth-over-time)
    denied: set[str]                        # operator-denied sources (#187)


_ASSETS_CACHE = VersionCache(maxsize=2)


def _jload(v: Any) -> list:
    if isinstance(v, str):
        return json.loads(v) if v else []
    return list(v) if v else []


async def _load_assets(pool: Any) -> _Assets:
    version = await data_version(pool)
    cached = _ASSETS_CACHE.get("assets", version)
    if cached is not None:
        return cached
    clusters = await pool.fetch(
        "select id, fact, claim_ids, originators, extremity, grounding from clusters"
    )
    claim_rows = await pool.fetch("select id, text, disputed from claims")
    art_rows = await pool.fetch("select id, source from articles")
    flag_rows = await pool.fetch(
        "select data from events where type = 'admin.source.flagged' order by id"
    )
    # Latest English pivot per claim (#240) — same bounded distinct-on read as stories (#283).
    pivot_rows = await pool.fetch(
        "select distinct on (data->>'claim_id') data from events "
        "where type = 'claim.pivot' and data->>'claim_id' <> '' and data->>'text_en' <> '' "
        "order by data->>'claim_id', id desc"
    )
    history = await load_trajectory(pool)

    id_to_source = {r["id"]: r["source"] for r in art_rows}
    text_by_claim = {str(r["id"]): r["text"] for r in claim_rows}
    disputed_claims = {str(r["id"]) for r in claim_rows if r["disputed"]}
    pivots: dict[str, str] = {}
    for r in pivot_rows:
        d = json.loads(r["data"]) if isinstance(r["data"], str) else (r["data"] or {})
        if d.get("claim_id") and d.get("text_en"):
            pivots[d["claim_id"]] = d["text_en"]

    facts: list[CorpusFact] = []
    claim_ids: dict[str, list[str]] = {}
    for cl in clusters:
        cids = [str(x) for x in _jload(cl["claim_ids"])]
        claim_ids[cl["id"]] = cids
        fact = cl["fact"] or ""
        # The fact's English pivot, via the member claim whose text IS the fact (stories' trick).
        embed_text = next(
            (pivots[c] for c in cids
             if (text_by_claim.get(c) or "").strip() == fact.strip() and c in pivots),
            "",
        )
        groups = [
            sorted({id_to_source.get(a, a) for a in grp}) for grp in _jload(cl["originators"])
        ]
        facts.append(CorpusFact(
            cluster_id=cl["id"], fact=fact, extremity=cl["extremity"] or "notable",
            grounding=cl["grounding"], disputed=any(c in disputed_claims for c in cids),
            originator_sources=groups, embed_text=embed_text,
        ))

    embeds: np.ndarray | None = None
    if facts:
        try:
            vecs = await asyncio.to_thread(
                mistral_embed, [f.embed_text or f.fact for f in facts]
            )
            embeds = np.asarray(vecs, dtype=np.float64)
        except Exception:  # noqa: BLE001 - no embeddings → corpus leg off; live leg still runs
            embeds = None

    assets = _Assets(
        facts=facts,
        claim_ids=claim_ids,
        embeds=embeds,
        reputation={
            r.source: reputation_score(r) for r in fold_reputation(history) if r.outcome_n > 0
        },
        denied=denied_sources([r["data"] for r in flag_rows]),
    )
    _ASSETS_CACHE.put("assets", version, assets)
    return assets


async def _hydrate(pool: Any, assets: _Assets, cluster_ids: set[str]) -> dict[str, CorpusFact]:
    """Members + bodies for the MATCHED clusters only — one bounded query, never the whole corpus."""
    wanted = [cid for cid in cluster_ids if cid in assets.claim_ids]
    all_claims = [c for cid in wanted for c in assets.claim_ids[cid]]
    if not all_claims:
        return {}
    rows = await pool.fetch(
        "select c.id::text as id, c.text, c.article_id, a.source, a.body "
        "from claims c join articles a on a.id = c.article_id where c.id::text = any($1::text[])",
        all_claims,
    )
    by_claim = {r["id"]: r for r in rows}
    out: dict[str, CorpusFact] = {}
    for fact in assets.facts:
        if fact.cluster_id not in cluster_ids:
            continue
        members: list[ClaimRow] = []
        bodies: dict[str, str] = {}
        for cid in assets.claim_ids.get(fact.cluster_id, []):
            r = by_claim.get(cid)
            if r is None:
                continue
            members.append(ClaimRow(id=cid, text=r["text"], article_id=r["article_id"],
                                    source=r["source"] or ""))
            bodies[r["article_id"]] = r["body"] or ""
        out[fact.cluster_id] = replace(fact, member_claims=members, bodies=bodies)
    return out


def make_corpus_lookup(loop: asyncio.AbstractEventLoop, pool: Any, assets: _Assets):
    """The pipeline's CorpusLookup seam, callable from the analysis worker thread: match against
    the cached embedding matrix, then hydrate the (few) matches over the event loop."""

    def lookup(texts) -> list[CorpusFact | None]:
        if assets.embeds is None or not assets.facts:
            return [None] * len(texts)
        hits = match_claims(list(texts), assets.facts, embed=mistral_embed,
                            corpus_embeddings=assets.embeds)
        matched = {assets.facts[m].cluster_id for m in hits if m is not None}
        if not matched:
            return [None] * len(texts)
        hydrated = asyncio.run_coroutine_threadsafe(
            _hydrate(pool, assets, matched), loop
        ).result(timeout=60)
        return [
            hydrated.get(assets.facts[m].cluster_id) if m is not None else None for m in hits
        ]

    return lookup


# ── live search (Apify primary, GDELT fallback — the feed's own channels) ───────────────────────

_STOPWORDS = frozenset(
    "the a an and or of in on at to for with from by is are was were has have had this that "
    "its his her their be been will would could should about into over after before".split()
)


def gdelt_query(text: str, *, max_terms: int = 6) -> str:
    """A GDELT keyword query from a claim: the first few significant words (plain terms AND)."""
    terms = [t for t in re.findall(r"[A-Za-z]{4,}", text) if t.lower() not in _STOPWORDS]
    return " ".join(terms[:max_terms])


def make_searcher() -> Callable[[str], list[LiveCandidate]]:
    def search(query: str) -> list[LiveCandidate]:
        out: list[LiveCandidate] = []
        if apify.available():
            try:
                out.extend(
                    LiveCandidate(url=a.url, domain=a.domain, title=a.title, body=a.body)
                    for a in apify.search_and_fetch(query, max_results=6)
                )
            except Exception:  # noqa: BLE001 - a failed search leg never sinks the analysis
                pass
        if len(out) < 3:  # thin/no Apify → GDELT metadata + our own fetcher for bodies
            try:
                arts = gdelt.search(gdelt_query(query), maxrecords=6, timespan="7d", retries=1)
                for a in arts[:4]:
                    body, _image = fetch_article(a.url)
                    if body:
                        out.append(LiveCandidate(url=a.url, domain=a.domain,
                                                 title=a.title, body=body))
            except Exception:  # noqa: BLE001
                pass
        return out

    return search


def make_accept(denied: set[str]):
    def accept(c: LiveCandidate) -> bool:
        if prefiltered_reject(c.domain):
            return False
        return c.domain not in denied and canonical_source(c.domain) not in denied

    return accept


# ── public payload ("what, not how") ─────────────────────────────────────────────────────────────


def public_claim(r: ClaimReading) -> dict[str, Any]:
    return {
        "text": r.claim.text,
        "voice": r.claim.voice,
        "speaker": r.claim.speaker,
        "central": bool(r.claim.in_headline or r.claim.is_synthesis),
        "extremity": r.extremity,
        "score": round(r.confidence * 100),
        "verdict": r.verdict,
        "tier": r.tier,
    }


def public_projection(r: ClaimReading) -> dict[str, Any]:
    return {"text": r.claim.text, "speaker": r.claim.speaker, "verdict": r.verdict}


def public_reasons(analysis: ArticleAnalysis) -> list[str]:
    """The overall score's drivers, in public wording — verdict-level language only, no mechanism.
    A disqualified score's own reasons are already phrased for the reader."""
    if analysis.score.band == "disqualified":
        return list(analysis.score.why)
    facts = analysis.facts
    if not facts:
        return ["no checkable factual claims yet"]
    reasons: list[str] = []
    central = [r for r in facts if r.claim.in_headline or r.claim.is_synthesis]
    anchor = min(central, key=lambda r: r.confidence) if central else max(
        facts, key=lambda r: r.independent_originators
    )
    if anchor.independent_originators <= 1 and not anchor.has_primary:
        reasons.append("the central claim has only this source so far")
    else:
        reasons.append(f"its central claim is {anchor.verdict[0].lower()}{anchor.verdict[1:]}")
    strong = sum(1 for r in facts if r is not anchor and r.confidence >= 0.70)
    if strong:
        reasons.append(f"{strong} corroborating fact{'s' if strong != 1 else ''} elsewhere in the article")
    if anchor.extremity in ("significant", "extraordinary"):
        reasons.append(f"holds its {anchor.extremity} central claim to a higher bar")
    if analysis.score.capped:
        reasons.append("reported so far only by sources without an established track record")
    return reasons


def public_payload(analysis: ArticleAnalysis, aid: str) -> dict[str, Any]:
    pub = analysis.publisher_score
    return {
        "analysis_id": aid,
        "url": analysis.url,
        "source": analysis.source,
        "title": analysis.title,
        "language": analysis.language,
        "date": analysis.date,
        "publisher": {
            "domain": analysis.source,
            "rated": pub is not None,
            "score": round(pub * 100) if pub is not None else None,
        },
        "overall": {
            "score": analysis.score.score,
            "band": analysis.score.band,
            "label": analysis.score.label,
            "reasons": public_reasons(analysis),
            "capped": analysis.score.capped,
            "forecast_only": analysis.score.forecast_only,
        },
        "claims": [public_claim(r) for r in analysis.facts],
        "projections": [public_projection(r) for r in analysis.projections],
        "scope": SCOPE_LINE,
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }


# ── run + cache ──────────────────────────────────────────────────────────────────────────────────


def _cache_put(aid: str, payload: dict) -> None:
    _RESULTS[aid] = (time.monotonic(), payload)
    _RESULTS.move_to_end(aid)
    while len(_RESULTS) > _RESULTS_MAX:
        _RESULTS.popitem(last=False)


def _fresh(analysed_at: str | None) -> bool:
    if not analysed_at:
        return False
    try:
        ts = datetime.fromisoformat(analysed_at)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - ts).total_seconds() < _TTL_S


async def _stored_payload(pool: Any, aid: str) -> dict | None:
    """A completed analysis from the events log (the durable cache) — newest wins."""
    try:
        row = await pool.fetchrow(
            "select data from events where type = 'analysis.completed' "
            "and data->>'analysis_id' = $1 order by id desc limit 1",
            aid,
        )
    except Exception:  # noqa: BLE001 - a cache-read failure just means re-analyse
        return None
    if row is None:
        return None
    d = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
    return d.get("analysis") if isinstance(d, dict) else None


async def cached_payload(pool: Any, aid: str) -> dict | None:
    hit = _RESULTS.get(aid)
    if hit is not None:
        return hit[1]
    stored = await _stored_payload(pool, aid)
    if stored is not None:
        _cache_put(aid, stored)
    return stored


async def run_analysis(
    state: Any, url: str, *, refresh: bool = False,
    progress: Callable[[str, dict], None] | None = None,
) -> dict:
    """Analyse ``url`` (or serve the fresh cached result), returning the public payload.
    Raises AnalyseError with a user-facing message for anything the caller did wrong; any
    other exception is internal and must be masked at the wire."""
    norm = normalise_url(url)
    aid = analysis_id(norm)
    pool = state.pool
    if not refresh:
        payload = await cached_payload(pool, aid)
        if payload is not None and _fresh(payload.get("analysed_at")):
            return payload
    err = await check_url(norm)
    if err:
        raise AnalyseError(err)

    async with _SEM:  # bound concurrent LLM analyses; queued requests wait their turn
        assets = await _load_assets(pool)
        loop = asyncio.get_running_loop()
        lookup = make_corpus_lookup(loop, pool, assets)
        analysis = await asyncio.to_thread(
            analyse_article,
            norm,
            reputation=assets.reputation,
            corpus_lookup=lookup,
            search=make_searcher() if _LIVE else None,
            accept_candidate=make_accept(assets.denied),
            live_max_searches=_MAX_SEARCHES,
            live_max_candidates=_MAX_CANDIDATES,
            progress=progress,
        )
    payload = public_payload(analysis, aid)
    _cache_put(aid, payload)
    nats = getattr(state, "nats", None)
    if nats is not None:
        try:  # durable cache + audit trail; best-effort, never blocks the response
            await events_mod.publish(
                nats, "analysis.completed", aid,
                {"analysis_id": aid, "url": norm, "analysis": payload},
                tenant_id=events_mod.PUBLIC_TENANT,
            )
        except Exception:  # noqa: BLE001
            pass
    return payload


# ── the router ───────────────────────────────────────────────────────────────────────────────────


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _public_progress(kind: str, data: dict) -> dict:
    """Progress events cross the public wire — map internal readings to the public claim shape."""
    if kind == "claim":
        return {
            "index": data.get("index"),
            "total": data.get("total"),
            "claim": public_claim(data["reading"]),
        }
    return data


# Module level (not inside _make_router): with `from __future__ import annotations` FastAPI
# resolves the endpoint's string annotations against module globals — a router-local class is
# invisible there and the body param degrades to a query param (social_api.py precedent).
if BaseModel is not None:

    class AnalyseReq(BaseModel):
        url: str = Field(min_length=8, max_length=2048)
        refresh: bool = False


def _make_router():
    router = APIRouter(prefix="/api/v2", tags=["analyse-v2"])

    @router.post("/analyse")
    async def analyse_endpoint(req: AnalyseReq, request: Request):
        # Strict per-IP budget — an analysis is real LLM + search spend (the global public
        # limiter stays on translate/feedback; this one is this route's own, much tighter).
        if not _LIMITER.allow(client_ip(request.scope)):
            return JSONResponse(
                {"detail": "analysis rate limit exceeded — try again shortly"},
                status_code=429,
                headers={"Retry-After": str(_LIMITER.retry_after())},
            )
        state = request.app.state
        aid = analysis_id(req.url)
        queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def progress(kind: str, data: dict) -> None:  # called from the worker thread
            loop.call_soon_threadsafe(queue.put_nowait, (kind, data))

        async def runner() -> None:
            try:
                payload = await run_analysis(state, req.url, refresh=req.refresh,
                                             progress=progress)
                queue.put_nowait(("done", {"analysis": payload}))
            except AnalyseError as e:  # user-facing by contract — safe to show verbatim
                queue.put_nowait(("error", {"detail": str(e)}))
            except Exception:  # noqa: BLE001 - internal: log to the container, mask on the wire
                traceback.print_exc()
                queue.put_nowait(("error", {"detail": "analysis failed — try again shortly"}))

        async def stream():
            task = asyncio.create_task(runner())
            try:
                yield _sse("start", {"analysis_id": aid})
                while True:
                    try:
                        kind, data = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"  # hold proxies open through slow phases
                        continue
                    if kind in ("done", "error"):
                        yield _sse(kind, data)
                        return
                    if kind == "scored":
                        continue  # the final payload carries the score
                    yield _sse(kind, _public_progress(kind, data))
            finally:
                task.cancel()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.get("/analyse/{aid}")
    async def analysis_by_id(aid: str, request: Request):
        payload = await cached_payload(request.app.state.pool, aid)
        if payload is None:
            return JSONResponse({"detail": "no such analysis"}, status_code=404)
        return JSONResponse({"analysis_id": aid, "analysis": payload})

    return router


# Module-level router — mount with: app.include_router(analyse_router)
if APIRouter is not None:
    analyse_router = _make_router()
else:  # pragma: no cover
    analyse_router = None  # type: ignore[assignment]
