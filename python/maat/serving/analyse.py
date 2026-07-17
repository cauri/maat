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
import logging
import ipaddress
import json
import os
import re
import socket
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import numpy as np

from maat import events as events_mod
from maat.acquire import apify, gdelt, source_gate
from maat.acquire.fetch import FetchedPage, fetch_article, fetch_page
from maat.acquire.source_gate import prefiltered_reject
from maat.learning.reputation import SourceReputation, fold_reputation, reputation_score
from maat.learning.trajectory import load_hindsight, load_trajectory
from maat import config as config_mod
from maat.pipeline.analyse import (
    AnalyseError,
    ArticleAnalysis,
    Citation,
    ClaimReading,
    CorpusFact,
    LiveCandidate,
    ScoringKnobs,
    analyse_article,
    check_one_claim,
    match_claims,
    sanitise_body,
)
from maat import prompts as prompts_mod
from maat.pipeline.authority import AUTHORITY_SEARCH_PROMPT, parse_authority_citations
from maat.pipeline.claim import Claim
from maat.pipeline.corroborate import SAME_FACT_THRESHOLD, ClaimRow
from maat.pipeline.identity import canonical_source
from maat.pipeline.ownership import evidenced_ownership, fold_ownership
from maat.providers.seam import claude_web_search, mistral_embed
from maat.serving.buildcache import VersionCache, data_version
from maat.serving.ratelimit import PerIpRateLimiter, client_ip
from maat.serving.source_flags import denied_sources

try:  # same guard as serving/feed.py — importable without FastAPI for pure-fn tests
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse, StreamingResponse
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    APIRouter = Request = JSONResponse = StreamingResponse = BaseModel = Field = None  # type: ignore[assignment,misc]

log = logging.getLogger("maat.serving.analyse")

SCOPE_LINE = (
    "Maat measures whether this article's factual claims hold up against independent reporting"
    " — not tone, bias, or what it leaves out."
)

# ── knobs (box .env; defaults are production-sane) ───────────────────────────────────────────────
_TTL_S = int(os.environ.get("MAAT_ANALYSE_TTL", "21600"))          # re-analyse after 6h
_CONCURRENCY = int(os.environ.get("MAAT_ANALYSE_CONCURRENCY", "2"))  # LLM analyses in flight
_LIVE = os.environ.get("MAAT_ANALYSE_LIVE", "1") not in ("0", "false", "no")
# Web-search corroboration (#381) — the primary live path; Apify (_LIVE) is its fallback. On by
# default; turn off to run Apify-only. web_search_20260209 is available on Sonnet 4.6.
_WEB_SEARCH = os.environ.get("MAAT_ANALYSE_WEB_SEARCH", "1") not in ("0", "false", "no")
_SEARCH_MODEL = os.environ.get("MAAT_ANALYSE_SEARCH_MODEL", "claude-sonnet-4-6")
# Tiered authority (#434) — the source-of-truth-seeking leg, on by default (cauri: the FIRST level
# of fact checking). Runs in parallel with the general pass; off → exactly the pre-#434 behaviour.
_AUTHORITY = os.environ.get("MAAT_ANALYSE_AUTHORITY", "1") not in ("0", "false", "no")
# NLI entailment gate thresholds (#389) — tunable in prod without a deploy once the web_neutral
# recall-drift signal shows whether the gate is too strict. Model swap is the other lever
# (MAAT_NLI_MODEL, in pipeline/nli.py).
_NLI_ENTAIL_MIN = float(os.environ.get("MAAT_ANALYSE_NLI_ENTAIL_MIN", "0.5"))
_NLI_CONTRADICT_MIN = float(os.environ.get("MAAT_ANALYSE_NLI_CONTRADICT_MIN", "0.6"))
_GATED = os.environ.get("MAAT_ANALYSE_GATE", "1") not in ("0", "false", "no")
_MAX_SEARCHES = int(os.environ.get("MAAT_ANALYSE_MAX_SEARCHES", "10"))
_MAX_CANDIDATES = int(os.environ.get("MAAT_ANALYSE_MAX_CANDIDATES", "18"))
_BODY_CHARS = int(os.environ.get("MAAT_ANALYSE_BODY_CHARS", "60000"))
# Reputation floor (cauri): a source needs at least this many resolved terminal outcomes
# (confirmed/refuted facts) before Maat rests a reputation on it — we never rate a publisher on
# too little info. Below the floor the source is treated as unproven everywhere reputation is read
# (the publisher rating, each claim's track-record flag, and the cold-start score cap).
_REPUTATION_FLOOR = int(os.environ.get("MAAT_ANALYSE_REPUTATION_FLOOR", "10"))
# An analysis costs real LLM + search work, so the per-IP budget is strict: a small burst, then
# one every five minutes. GETs of finished analyses are NOT limited (bounded, cached reads).
_LIMITER = PerIpRateLimiter(
    capacity=float(os.environ.get("MAAT_ANALYSE_RATE_BURST", "3")),
    refill_per_sec=float(os.environ.get("MAAT_ANALYSE_RATE_RPS", str(1 / 300))),
)
# Force-checking one claim (#397) is a fraction of a full analysis (one web-search pass, no
# extraction/classification), so its budget is looser — a reader clearing the "Not checked" rows on
# a page shouldn't hit the whole-analysis wall after three.
_CHECK_LIMITER = PerIpRateLimiter(
    capacity=float(os.environ.get("MAAT_ANALYSE_CHECK_BURST", "12")),
    refill_per_sec=float(os.environ.get("MAAT_ANALYSE_CHECK_RPS", str(1 / 30))),
)
_SEM = asyncio.Semaphore(max(1, _CONCURRENCY))

_RESULTS_MAX = 256
_RESULTS: OrderedDict[str, tuple[float, dict]] = OrderedDict()  # id -> (monotonic ts, payload)
# Strong references to decoupled analysis tasks so a client disconnect can't cancel them and the
# GC can't collect a task no one awaits (#391). Cleared by each task's done-callback.
_INFLIGHT: set[asyncio.Task] = set()


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


_VARIANT_PREFIXES = ("www.", "amp.", "m.", "mobile.")


def _strip_variant(host: str) -> str:
    h = (host or "").lower().rstrip(".")
    for p in _VARIANT_PREFIXES:
        if h.startswith(p):
            return h[len(p):]
    return h


def _same_publisher(host_a: str, host_b: str) -> bool:
    """Do two hostnames belong to the same publisher? Deliberately conservative — a false 'no'
    just means we re-analyse; a false 'yes' would let a page borrow another outlet's identity."""
    ha, hb = _strip_variant(host_a), _strip_variant(host_b)
    if ha and ha == hb:
        return True
    if ha and hb and (ha.endswith("." + hb) or hb.endswith("." + ha)):  # subdomain of the other
        return True
    ca = canonical_source(host_a)                       # both resolve to the same KNOWN outlet
    return ca == canonical_source(host_b) and "." not in ca  # registry slug (no dot) vs passthrough


def identity_url(pasted_norm: str, page: FetchedPage) -> str:
    """The URL a paste is CACHED under: the page's own canonical URL when it declares one AND it
    belongs to the same publisher (AMP / mobile / share / tracking variants collapse to one
    entry), else the normalised pasted URL.

    The same-publisher guard is a REPUTATION-LAUNDERING defence: a page must not claim another
    outlet's canonical to be analysed — and scored — as that outlet. Cross-publisher syndication
    dedup is a separate, content-based problem (not this)."""
    canon = (page.canonical or "").strip()
    if not canon:
        return pasted_norm
    try:
        cp = urlparse(canon)
    except ValueError:
        return pasted_norm
    if cp.scheme not in ("http", "https") or not cp.hostname:
        return pasted_norm
    if not _same_publisher(urlparse(pasted_norm).hostname or "", cp.hostname):
        return pasted_norm
    return normalise_url(canon)


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
    ownership: dict[str, str]               # canonical source -> ownership group (#41/#254)
    # Promoted scoring overrides (#412): the operator Config panel's sign-off-gated knobs, folded
    # from admin.config.promoted — the same flow the feed's corroborate agent honours.
    knobs: ScoringKnobs = ScoringKnobs()
    same_fact: float = SAME_FACT_THRESHOLD  # cluster.same_fact — the §5.4 bar, also promotable
    # #434 — the authority-search prompt, resolved from the prompt store (operator-editable via
    # /prompts, seed = the DRAFT in pipeline/authority.py) once per assets build, like the knobs.
    authority_prompt: str = AUTHORITY_SEARCH_PROMPT


_ASSETS_CACHE = VersionCache(maxsize=2)


def _jload(v: Any) -> list:
    if isinstance(v, str):
        return json.loads(v) if v else []
    return list(v) if v else []


def build_reputation_map(
    recs: Iterable[SourceReputation], *, floor: int
) -> dict[str, float]:
    """Collapse fold_reputation records into the ``{source: 0..1 standing}`` map the analysis reads.

    Admits only sources whose track record rests on at least ``floor`` resolved terminal outcomes
    (confirmed/refuted facts) — Maat never rests a reputation on too little info (cauri). Keyed by
    the raw source string AND its canonical id (§6.7) so a pasted ``bbc.co.uk`` finds a record
    stored under ``bbc.com`` / "BBC News"; on a canonical collision the record with the most
    resolved outcomes speaks for the outlet.
    """
    rated = [r for r in recs if r.outcome_n >= floor]
    reputation: dict[str, float] = {r.source: reputation_score(r) for r in rated}
    best: dict[str, tuple[int, float]] = {}
    for r in rated:
        canon = canonical_source(r.source)
        if canon not in best or r.outcome_n > best[canon][0]:
            best[canon] = (r.outcome_n, reputation_score(r))
    for canon, (_n, score) in best.items():
        reputation.setdefault(canon, score)
    return reputation


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
    # Ownership grouping (#41/#254): co-owned outlets collapse to one independent originator — the
    # SAME anti-laundering rollup the feed's corroborate agent applies. Auto-resolved Wikidata graph
    # UNDER the operator's manual groups, which override it (a wrong auto-merge would hide real
    # corroboration, so the operator always wins).
    owner_rows = await pool.fetch(
        "select data from events where type = 'source.ownership.resolved' order by id"
    )
    grouped_rows = await pool.fetch(
        "select distinct on (data->>'source') data->>'source' s, data->>'group' g "
        "from events where type = 'admin.source.grouped' order by data->>'source', id desc"
    )
    # Promoted scoring knobs (#412) — the sign-off-gated Config-panel overrides. Folded the same
    # way the feed's corroborate agent folds them, so one promote governs both surfaces.
    promoted_rows = await pool.fetch(
        "select data from events where type = 'admin.config.promoted' order by id"
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

    # Only sources whose track record rests on enough resolved outcomes are rated — Maat never
    # rests a reputation on too little info (cauri). Canonical-aware, so a pasted bbc.co.uk finds
    # a record stored under bbc.com / "BBC News".
    reputation = build_reputation_map(
        fold_reputation(history, hindsight=await load_hindsight(pool)),
        floor=_REPUTATION_FLOOR,
    )

    auto_owner = fold_ownership(
        json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in owner_rows
    )
    manual_owner = {canonical_source(r["s"]): r["g"] for r in grouped_rows if r["s"] and r["g"]}

    overrides = config_mod.analyse_overrides(
        config_mod.active_config(r["data"] for r in promoted_rows)
    )
    assets = _Assets(
        facts=facts,
        claim_ids=claim_ids,
        embeds=embeds,
        reputation=reputation,
        denied=denied_sources([r["data"] for r in flag_rows]),
        # #425: co-ownership collapses only with OBSERVED shared output (same-fact
        # co-occurrence; a shared refuted fact alone suffices). Manual operator groups stay
        # blanket and override — a human's explicit group never needs statistical evidence.
        ownership={**evidenced_ownership(auto_owner, history), **manual_owner},
        knobs=ScoringKnobs(**overrides["knobs"]),
        same_fact=overrides["same_fact_threshold"],
        authority_prompt=await prompts_mod.active_text(
            pool, "authority_search", prompts_mod.seed_default("authority_search")
        ),
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
                            corpus_embeddings=assets.embeds, threshold=assets.same_fact)
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
            except Exception as e:  # noqa: BLE001 - a failed search leg never sinks the analysis
                log.warning("apify search leg failed for %r: %s", query[:60], type(e).__name__)
        if len(out) < 3:  # thin/no Apify → GDELT metadata + our own fetcher for bodies
            try:
                arts = gdelt.search(gdelt_query(query), maxrecords=6, timespan="7d", retries=3)
                for a in arts[:4]:
                    body, _image = fetch_article(a.url)
                    if body:
                        out.append(LiveCandidate(url=a.url, domain=a.domain,
                                                 title=a.title, body=body))
            except Exception as e:  # noqa: BLE001
                log.warning("gdelt search leg failed for %r: %s", query[:60], type(e).__name__)
        return out

    return search


def make_accept(denied: set[str]):
    def accept(c: LiveCandidate) -> bool:
        if prefiltered_reject(c.domain):
            return False
        return c.domain not in denied and canonical_source(c.domain) not in denied

    return accept


# ── web-search corroboration (#381): the PRIMARY live path ──────────────────────────────────────
#
# One web-search-enabled call finds, per claim, independent sources + the VERBATIM sentence that
# asserts it. The pipeline re-fetches each cited page (extraction ladder), checks the quote is
# really there, then an NLI model judges entailment — the model only proposes; deterministic code +
# NLI decide. This replaced per-claim Apify search + per-candidate LLM extraction + cosine matching
# (the "Corroborated · 83" false-positive path). Apify stays wired as the per-claim fallback.
#
# ⚠️ PROMPT REVIEW (cauri): this is a NEW in-app agent prompt — a first cut following the repo
# prompt template (docs/prompt-template.md). Structure/tone up for review before it's treated as
# locked; it is the canonical seed, overridable later via the prompt store (P8) like the extractor.
_WEB_SEARCH_TOOL_TYPE = "web_search_20260209"

SEARCH_PROMPT = r"""# ROLE

You are a corroboration researcher for a news-veracity engine. Given the factual claims of ONE
article, you search the open web for INDEPENDENT reporting that asserts the SAME facts. You do not
judge whether a claim is true — you find who else reports it and quote them, word for word.

# GOALS

- For each claim, surface independent news sources that state the same fact, each with the exact
  sentence from that page which asserts it.

# INSTRUCTIONS

1. For each numbered claim, run web searches to find independent news reporting of that fact.
2. Prefer established news outlets and primary sources (an official body's own release). A source
   found while checking one claim may support another — reuse it wherever it applies.
3. For each supporting source, copy ONE sentence VERBATIM from that page — the exact words as they
   appear, no paraphrase, no ellipsis, no edits, no added or dropped words. If you cannot find a
   verbatim sentence on the page that asserts the claim, omit that source.

# GUIDELINES

- "Independent" means a DIFFERENT publisher from the article's own outlet. Never return the
  article's own publisher.
- A claim may have several supporting sources, or none. Return an empty list for a claim you
  cannot corroborate — do not stretch to fill it.
- Match the FACT, not just the topic. A page about a related but different matter is not support.

# GUARDRAILS

- Never invent a URL, a publisher, or a quote. Every quote must be text you actually read on the
  page at that URL; the engine re-fetches each page and discards any quote it cannot find there.
- Do not assess truth, tone, or bias; only find who else reports the fact and quote them verbatim.
- Do not return encyclopedias, wikis, social media, forums, or aggregators.

# OUTPUT FORMAT

A single JSON object and nothing else. Keys are the claim numbers as strings ("1" … "N"); each
value is an array of objects {"url": string, "domain": string, "quote": string}. The quote is
verbatim. Use an empty array for claims with no corroboration.

# CONTEXT

## ARTICLE PUBLISHER (never return its own pages)

{own_domain}

## CLAIMS

{claims}
"""


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except ValueError:
        return ""


def _blocks_text(blocks: list[dict]) -> str:
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def parse_citations(text: str, n: int) -> list[list[Citation]]:
    """Per-claim citations from the model's JSON reply (keys "1".."n"). Tolerant: pulls the JSON
    object out of any surrounding prose/fences, skips malformed entries, never raises. Missing or
    junk → an empty list for that claim (the claim then falls back to Apify)."""
    out: list[list[Citation]] = [[] for _ in range(n)]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return out
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return out
    if not isinstance(obj, dict):
        return out
    for key, items in obj.items():
        try:
            idx = int(str(key)) - 1
        except ValueError:
            continue
        if not (0 <= idx < n) or not isinstance(items, list):
            continue
        cits: list[Citation] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            quote = str(item.get("quote") or "").strip()
            if not url or not quote:
                continue
            domain = str(item.get("domain") or "").strip().removeprefix("www.") or _domain_of(url)
            cits.append(Citation(url=url, domain=domain, quote=quote))
        out[idx] = cits
    return out


# A single web-search call shares ONE server-side search budget across all its claims, so cramming
# ~15 claims into one call starves each — the model runs out of searches and central claims come
# back uncorroborated. Batch into small groups (each gets a full budget) and run the batches in
# PARALLEL — better coverage AND lower wall-clock (the batches overlap instead of summing).
_SEARCH_BATCH = int(os.environ.get("MAAT_ANALYSE_SEARCH_BATCH", "4"))


def _search_batch(
    claim_texts: Sequence[str], own_domain: str, blocked: list[str], *, deep: bool = False,
) -> list[list[Citation]]:
    """One web-search call over a small batch of claims → per-claim citations (aligned).

    ``deep`` (S3 #401) is the second-pass budget for claims that came back uncorroborated: it raises
    the search allowance so the model can try more angles before we conclude a claim is lone. The
    PROMPT is unchanged — only the tool's ``max_uses`` grows (a fresh, larger-budget pass), so this
    needs no in-app prompt change."""
    n = len(claim_texts)
    claims_block = "\n".join(f"{k + 1}. {t}" for k, t in enumerate(claim_texts))
    prompt = SEARCH_PROMPT.replace("{own_domain}", own_domain or "unknown").replace(
        "{claims}", claims_block
    )
    per_claim, base = (6, 4) if deep else (3, 2)
    tool: dict = {
        "type": _WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": per_claim * n + base,
    }
    if blocked:
        tool["blocked_domains"] = blocked
    try:
        blocks = claude_web_search(prompt, tools=[tool], model=_SEARCH_MODEL)
    except Exception as e:  # noqa: BLE001 - a failed batch leaves its claims to the Apify fallback
        log.warning("web-search batch of %d claims failed: %s", n, type(e).__name__)
        return [[] for _ in range(n)]
    return parse_citations(_blocks_text(blocks), n)


def make_web_search(denied: set[str]):
    """The pipeline's WebSearchFn: per-claim citations (url + verbatim quote) from web search.
    Claims are batched (each batch a separate call with its own search budget) and the batches run
    concurrently. Blocks the article's own outlet + denied sources at the tool level; the pipeline
    re-verifies domain, quote, and entailment regardless."""

    def web_search(claim_texts: Sequence[str], own_domain: str, deep: bool = False) -> list[list[Citation]]:
        n = len(claim_texts)
        if not n:
            return []
        blocked = sorted({d for d in (own_domain, *denied) if d})
        batches = [
            list(claim_texts[s : s + _SEARCH_BATCH]) for s in range(0, n, _SEARCH_BATCH)
        ]
        with ThreadPoolExecutor(max_workers=len(batches)) as ex:
            results = list(ex.map(
                lambda b: _search_batch(b, own_domain, blocked, deep=deep), batches
            ))
        out: list[list[Citation]] = []
        for r in results:
            out.extend(r)
        return out[:n] + [[] for _ in range(n - len(out))]

    return web_search


def _authority_batch(
    claim_texts: Sequence[str], own_domain: str, blocked: list[str], prompt_seed: str,
    *, deep: bool = False,
) -> list[list[Citation]]:
    """One authority-search call over a small batch of claims → per-claim TIERED citations.

    Same shape as ``_search_batch`` — same tool, same model, same budget arithmetic — but the
    prompt seeks each claim's authoritative PRIMARY (tier 1 = the document itself, tier 2 = the
    official statement) instead of press coverage, and the parser drops anything the model returns
    outside tiers 1–2. The pipeline then applies the identical NLI + grounding gates; survivors
    mark their URL primary for the fold (#434)."""
    n = len(claim_texts)
    claims_block = "\n".join(f"{k + 1}. {t}" for k, t in enumerate(claim_texts))
    prompt = prompt_seed.replace("{own_domain}", own_domain or "unknown").replace(
        "{claims}", claims_block
    )
    per_claim, base = (6, 4) if deep else (3, 2)
    tool: dict = {
        "type": _WEB_SEARCH_TOOL_TYPE, "name": "web_search", "max_uses": per_claim * n + base,
    }
    if blocked:
        tool["blocked_domains"] = blocked
    try:
        blocks = claude_web_search(prompt, tools=[tool], model=_SEARCH_MODEL)
    except Exception as e:  # noqa: BLE001 - a failed batch just leaves its claims to the general leg
        log.warning("authority-search batch of %d claims failed: %s", n, type(e).__name__)
        return [[] for _ in range(n)]
    return parse_authority_citations(_blocks_text(blocks), n)


def make_authority_search(denied: set[str], prompt_seed: str):
    """The pipeline's authority-seeking WebSearchFn (#434): per-claim tier-1/2 citations from
    primary-source-targeted search. Batched and concurrent like ``make_web_search``; blocks the
    article's own outlet + denied sources at the tool level; the pipeline re-verifies everything."""

    def authority_search(
        claim_texts: Sequence[str], own_domain: str, deep: bool = False
    ) -> list[list[Citation]]:
        n = len(claim_texts)
        if not n:
            return []
        blocked = sorted({d for d in (own_domain, *denied) if d})
        batches = [
            list(claim_texts[s : s + _SEARCH_BATCH]) for s in range(0, n, _SEARCH_BATCH)
        ]
        with ThreadPoolExecutor(max_workers=len(batches)) as ex:
            results = list(ex.map(
                lambda b: _authority_batch(b, own_domain, blocked, prompt_seed, deep=deep), batches
            ))
        out: list[list[Citation]] = []
        for r in results:
            out.extend(r)
        return out[:n] + [[] for _ in range(n - len(out))]

    return authority_search


def make_nli():
    """The pipeline's NLI seam (entailment judge) — the loaded cross-encoder, or None when the NLI
    model is unavailable (gated by MAAT_CONTRADICTION_NLI). None → the pipeline does not count
    web-search citations as corroboration (it never trusts the search model's own mapping) and
    falls back to Apify; it also flags the degradation in the audit meta."""
    from maat.pipeline import nli

    return nli.classify_pair if nli.available() else None


def make_gate():
    """Reject pages that are not news articles, with a plain answer for the reader.

    Hard non-news list first (free — wikis, social, forums); then the acquisition source-gate's
    own classifier (the SAME judgement that admits sources to the feed, prompt reused verbatim).
    Fails OPEN on classifier errors — a gate hiccup must never take the product down."""

    def gate(domain: str, title: str) -> str | None:
        if prefiltered_reject(domain):
            return (f"Maat weighs news articles — {domain} isn't a news publisher. "
                    "Paste a link to a news story.")
        try:
            verdict = source_gate.classify(domain, title, channel="analyse")
        except Exception:  # noqa: BLE001 - fail open: gate trouble must not block analyses
            return None
        if verdict is not None and not verdict.accept:
            return ("Maat weighs news articles — this page doesn't look like one. "
                    "Paste a link to a news story.")
        return None

    return gate


# ── public payload ("what, not how") ─────────────────────────────────────────────────────────────


def public_claim(r: ClaimReading) -> dict[str, Any]:
    # An unchecked claim (#397) was never searched — it carries NO score (a number would imply we
    # weighed it). ``checked`` lets the page render the "Not checked" state + its force-check button.
    return {
        "text": r.claim.text,
        "voice": r.claim.voice,
        "speaker": r.claim.speaker,
        "central": bool(r.claim.in_headline or r.claim.is_synthesis),
        "extremity": r.extremity,
        "score": round(r.confidence * 100) if r.checked else None,
        "verdict": r.verdict,
        "tier": r.tier,
        "checked": r.checked,
    }


def public_projection(r: ClaimReading) -> dict[str, Any]:
    return {"text": r.claim.text, "speaker": r.claim.speaker, "verdict": r.verdict}


def public_reasons(analysis: ArticleAnalysis) -> list[str]:
    """The overall score's drivers, in public wording — verdict-level language only, no mechanism.
    A disqualified score's own reasons are already phrased for the reader."""
    if analysis.score.band == "disqualified":
        return list(analysis.score.why)
    # Reason over CHECKED facts only (#397) — an unchecked claim carries no verdict to anchor on, and
    # its confidence of 0 would falsely read as the weakest central claim.
    facts = [r for r in analysis.facts if r.checked]
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


def _trim(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _tally_from_verdicts(verdicts: list[str]) -> dict[str, int]:
    """Headline counts for the card + captions (verdict-level, no mechanism)."""
    t = {"total": len(verdicts), "corroborated": 0, "single_source": 0, "disputed": 0, "primary": 0}
    for v in verdicts:
        if v.startswith(("Well corroborated", "Corroborated")):
            t["corroborated"] += 1
        elif v.startswith("Disputed"):
            t["disputed"] += 1
        elif v.startswith("Only this source"):
            t["single_source"] += 1
        elif v.startswith("Stated by the primary"):
            t["primary"] += 1
    return t


def claim_tally(facts: list[ClaimReading]) -> dict[str, int]:
    return _tally_from_verdicts([r.verdict for r in facts])


def _build_share(*, title: str | None, source: str, label: str, score: int, forecast_only: bool,
                 top_reason: str, pub_score_100: int | None, tally: dict[str, int]) -> dict[str, Any]:
    """Per-platform share text, templated from the ruling — deterministic, defensible, and
    "what, not how" (says the claims weren't corroborated by independent reporting, never the
    mechanism; never overstates a named publisher's article as "false"). Measured tone.

    Pure over primitives so it runs from a live ArticleAnalysis OR a stored payload dict (an
    analysis cached before this feature existed backfills its share block on read)."""
    disp = _trim(title or "this article", 90)
    headline = label if forecast_only else f"{label} · {score}/100"
    top = _cap(top_reason) if top_reason else ""
    pub_rec = f" (track record {pub_score_100}%)" if pub_score_100 is not None else ""
    og_description = _trim(
        f"{headline}. " + (f"{top}. " if top else "")
        + "Maat weighs each factual claim against independent reporting — not tone or bias.",
        200,
    )
    twitter_text = _trim(
        f"I ran “{_trim(title or 'this article', 64)}” through Maat: {headline}."
        + (f" {top}." if top else "") + " Weigh any article yourself →",
        240,
    )
    linkedin_text = (
        "I checked this article with Maat, which weighs how well a story's factual claims hold "
        "up against independent reporting.\n\n"
        f"Verdict: {headline}."
        + (f"\nKey finding: {top}." if top else "")
        + (f"\nPublisher: {source}{pub_rec}." if source else "")
        + "\n\nMaat weighs claims, not tone or bias. Weigh any article at maat.press/analyse"
    )
    instagram_caption = (
        f"Maat weighed “{disp}”: {headline}." + (f" {top}." if top else "")
        + "\n\nMaat scores how well a news story's factual claims hold up against independent "
        "reporting — not its tone or bias.\n\n"
        "Weigh any article yourself — link in bio (maat.press/analyse)\n\n"
        "#news #medialiteracy #factcheck #journalism #press #maat"
    )
    return {
        "headline": headline,
        "tally": tally,
        "og_title": _trim(f"Maat weighed “{disp}”", 90),
        "og_description": og_description,
        "twitter_text": twitter_text,
        "linkedin_text": linkedin_text,
        "instagram_caption": instagram_caption,
    }


def share_copy(analysis: ArticleAnalysis, reasons: list[str]) -> dict[str, Any]:
    return _build_share(
        title=analysis.title, source=analysis.source, label=analysis.score.label,
        score=analysis.score.score, forecast_only=analysis.score.forecast_only,
        top_reason=reasons[0] if reasons else "",
        pub_score_100=(round(analysis.publisher_score * 100)
                       if analysis.publisher_score is not None else None),
        tally=claim_tally(analysis.facts),
    )


def share_copy_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the share block from a stored public payload — for analyses cached before the
    share feature shipped, so a shared link to any of them still unfurls + renders a card."""
    o = payload.get("overall", {})
    reasons = o.get("reasons") or [""]
    return _build_share(
        title=payload.get("title"), source=payload.get("source", ""), label=o.get("label", ""),
        score=int(o.get("score", 0) or 0), forecast_only=bool(o.get("forecast_only", False)),
        top_reason=reasons[0], pub_score_100=(payload.get("publisher") or {}).get("score"),
        tally=_tally_from_verdicts([c.get("verdict", "") for c in payload.get("claims", [])]),
    )


def ops_meta(analysis: ArticleAnalysis, *, secs: float | None = None) -> dict[str, Any]:
    """Operator-only coverage meta for the ``analysis.completed`` audit event (#382): what the live
    pass actually covered and HOW corroboration was reached (web vs Apify fallback, contradictions,
    NLI availability, injection-guard drops, wall-clock). NEVER merged into the public payload —
    the served cache reads only the ``analysis`` key, so this stays off the wire ("what, not how")."""
    out: dict[str, Any] = {"dropped_claims": analysis.dropped_claims,
                           "merged_claims": analysis.merged_claims}
    if secs is not None:
        out["secs"] = secs
    if analysis.live is not None:
        lv = analysis.live
        out["live"] = {
            "searched_claims": lv.searched_claims,
            "skipped_claims": lv.skipped_claims,
            "candidates_considered": lv.candidates_considered,
            "candidates_used": lv.candidates_used,
            "web_corroborated": lv.web_corroborated,
            "web_contradicted": lv.web_contradicted,
            "web_neutral": lv.web_neutral,
            "apify_fallbacks": lv.apify_fallbacks,
            "nli_available": lv.nli_available,
            "deep_searched": lv.deep_searched,
            "deep_rescued": lv.deep_rescued,
        }
    return out


def public_payload(analysis: ArticleAnalysis, aid: str) -> dict[str, Any]:
    pub = analysis.publisher_score
    reasons = public_reasons(analysis)
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
            # Flipped true by run_analysis when this analysis put the publisher into the
            # source-registry review pipeline (backfill its history → score its track record).
            "review_started": False,
        },
        "overall": {
            "score": analysis.score.score,
            "band": analysis.score.band,
            "label": analysis.score.label,
            "reasons": reasons,
            "capped": analysis.score.capped,
            "forecast_only": analysis.score.forecast_only,
            # Claims skipped over the live cap and never searched (#397): scored by no one, shown as
            # "Not checked", each force-checkable. Surfaced so the page can say "N not checked".
            "unchecked": sum(1 for r in analysis.facts if not r.checked),
        },
        "claims": [public_claim(r) for r in analysis.facts],
        "projections": [public_projection(r) for r in analysis.projections],
        "share": share_copy(analysis, reasons),
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
    """A completed analysis from the events log (the durable cache) — newest wins.

    Keyed on ``stream_id`` (== the analysis id, set at publish time), so this is an indexed
    point-lookup via the existing ``events_stream_idx (stream_id, id)`` — no scan over the shared
    log as analyses accumulate, and no redundant index to maintain."""
    try:
        row = await pool.fetchrow(
            "select data from events where stream_id = $1 and type = 'analysis.completed' "
            "order by id desc limit 1",
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
        # Backfill the share block for analyses cached before the feature shipped, so a shared
        # link to any of them still unfurls its OG card + renders the image (which read `share`).
        if isinstance(stored, dict) and "share" not in stored:
            stored["share"] = share_copy_from_payload(stored)
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
    # 1) Exact re-paste (incl. tracking/fragment/case variants → same normalised URL): served
    #    from cache with no fetch at all.
    if not refresh:
        payload = await cached_payload(pool, aid)
        if payload is not None and _fresh(payload.get("analysed_at")):
            return payload
    err = await check_url(norm)
    if err:
        raise AnalyseError(err)

    # 2) Fetch once (outside the analysis gate — a canonical cache hit shouldn't hold a slot). The
    #    page's declared canonical URL collapses AMP/mobile/share variants of ONE article (same
    #    publisher) onto a single cache identity, so the second variant skips the ~minutes of LLM
    #    work even though it still pays the ~seconds of fetch.
    page = await asyncio.to_thread(fetch_page, norm)
    if page is None or not page.body:
        raise AnalyseError("could not extract an article from this URL")
    ident = identity_url(norm, page)
    canon_aid = analysis_id(ident)
    if canon_aid != aid and not refresh:
        payload = await cached_payload(pool, canon_aid)
        if payload is not None and _fresh(payload.get("analysed_at")):
            _cache_put(aid, payload)  # alias the pasted variant → canonical (fast next time, this process)
            return payload

    async with _SEM:  # bound concurrent LLM analyses; queued requests wait their turn
        assets = await _load_assets(pool)
        loop = asyncio.get_running_loop()
        lookup = make_corpus_lookup(loop, pool, assets)
        t_analyse = time.monotonic()
        analysis = await asyncio.to_thread(
            analyse_article,
            ident,                          # identity = canonical when collapsed (correct publisher)
            reputation=assets.reputation,
            ownership=assets.ownership,
            corpus_lookup=lookup,
            web_search=make_web_search(assets.denied) if (_LIVE and _WEB_SEARCH) else None,
            authority_search=(
                make_authority_search(assets.denied, assets.authority_prompt)
                if (_LIVE and _WEB_SEARCH and _AUTHORITY) else None
            ),
            nli=make_nli(),
            search=make_searcher() if _LIVE else None,
            accept_candidate=make_accept(assets.denied),
            gate=make_gate() if _GATED else None,
            # Reuse the already-fetched pasted page for the pasted URL only; cited-page verification
            # (#381) uses the FAST ladder rungs for every OTHER URL (no Apify/Zyte per cited URL —
            # a walled citation falls back to the NLI judgement rather than paying the slow rungs).
            fetch=lambda u: page if u == ident else fetch_page(u, fast=True),
            nli_entail_min=_NLI_ENTAIL_MIN,
            nli_contradict_min=_NLI_CONTRADICT_MIN,
            live_max_searches=_MAX_SEARCHES,
            live_max_candidates=_MAX_CANDIDATES,
            body_max_chars=_BODY_CHARS,
            same_fact_threshold=assets.same_fact,
            knobs=assets.knobs,   # promoted Config-panel overrides (#412)
            progress=progress,
        )
    payload = public_payload(analysis, canon_aid)
    nats = getattr(state, "nats", None)
    # An unrated publisher enters the EXISTING reliability-review pipeline (#241): registering
    # it queues the source-registry agent's backfill of its past reporting → a scored track
    # record next time. Idempotent — already-registered sources are left alone.
    if nats is not None and payload["publisher"]["rated"] is False and analysis.source:
        try:
            if not await _registry_seen(pool, analysis.source):
                await events_mod.publish(
                    nats, events_mod.SOURCE_REGISTERED, analysis.source,
                    {"source": analysis.source, "state": "registered", "provider": "analyse",
                     "at": payload["analysed_at"]},
                )
            payload["publisher"]["review_started"] = True
        except Exception:  # noqa: BLE001 - review kickoff is best-effort
            pass
    # Cache + persist under the CANONICAL id (shares/links use it); alias the pasted variant in
    # this process so a same-process re-paste of the AMP/mobile URL is instant too.
    _cache_put(canon_aid, payload)
    if canon_aid != aid:
        _cache_put(aid, payload)
    if nats is not None:
        try:  # durable cache + audit trail; best-effort, never blocks the response
            await events_mod.publish(
                nats, "analysis.completed", canon_aid,
                {
                    "analysis_id": canon_aid, "url": ident, "analysis": payload,
                    # Operator-only coverage meta (#382): HOW corroboration was reached — never
                    # part of `analysis` (the public payload / cache read exactly that key), so
                    # "what, not how" holds on the wire while the audit trail keeps the how.
                    "ops": ops_meta(analysis, secs=round(time.monotonic() - t_analyse, 1)),
                },
                tenant_id=events_mod.PUBLIC_TENANT,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("analysis.completed publish failed for %s: %s", canon_aid, type(e).__name__)
    return payload


async def run_check(
    state: Any, url: str, text: str, *,
    voice: str = "own", speaker: str | None = None, central: bool = False,
) -> dict:
    """Force-check ONE claim on demand (#397): run the live corroboration path for a single claim the
    batch analysis skipped over the cap, and return its public claim shape — now with a real verdict
    and (if corroborated) a score. Raises AnalyseError (user-facing) for a bad URL; other exceptions
    are internal and masked at the wire. Bounded by the same LLM-work semaphore as full analyses so a
    burst of checks can't overrun the box."""
    norm = normalise_url(url)
    err = await check_url(norm)
    if err:
        raise AnalyseError(err)
    page = await asyncio.to_thread(fetch_page, norm)
    if page is None or not page.body:
        raise AnalyseError("could not extract an article from this URL")
    source = urlparse(norm).netloc.removeprefix("www.")
    body = sanitise_body(page.body, max_chars=_BODY_CHARS)
    claim = Claim(
        text=text, voice=("attributed" if voice == "attributed" else "own"),
        speaker=speaker, evidence_span=text, in_headline=bool(central), kind="fact",
    )
    async with _SEM:
        assets = await _load_assets(state.pool)
        reading, _rated = await asyncio.to_thread(
            check_one_claim,
            claim,
            body=body,
            source=source,
            url=norm,
            reputation=assets.reputation,
            ownership=assets.ownership,
            web_search=make_web_search(assets.denied) if (_LIVE and _WEB_SEARCH) else None,
            authority_search=(
                make_authority_search(assets.denied, assets.authority_prompt)
                if (_LIVE and _WEB_SEARCH and _AUTHORITY) else None
            ),
            nli=make_nli(),
            search=make_searcher() if _LIVE else None,
            accept_candidate=make_accept(assets.denied),
            # Cited pages verify on the FAST ladder only (no Apify/Zyte per cited URL) — same posture
            # as the batch path; a walled citation falls back to the NLI judgement.
            fetch=lambda u: fetch_page(u, fast=True),
            nli_entail_min=_NLI_ENTAIL_MIN,
            nli_contradict_min=_NLI_CONTRADICT_MIN,
            live_max_candidates=_MAX_CANDIDATES,
            same_fact_threshold=assets.same_fact,
            knobs=assets.knobs,   # the forced path honours the same promoted knobs (#412)
        )
    return public_claim(reading)


async def _registry_seen(pool: Any, source: str) -> bool:
    """Is this source already in the registry lifecycle (#241)? Unknown → True (don't spam)."""
    try:
        row = await pool.fetchrow(
            "select 1 from events where type in ('source.registered','source.state_changed') "
            "and data->>'source' = $1 limit 1",
            source,
        )
        return row is not None
    except Exception:  # noqa: BLE001
        return True


# ── the router ───────────────────────────────────────────────────────────────────────────────────


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _public_progress(kind: str, data: dict) -> dict:
    """Progress events cross the public wire — map internal readings to the public claim shape.
    Counts and indices only, never source names (what, not how)."""
    if kind == "claim":
        return {
            "index": data.get("index"),
            "total": data.get("total"),
            "claim": public_claim(data["reading"]),
        }
    if kind == "checking":
        return {"index": data.get("index")}
    return data


# Module level (not inside _make_router): with `from __future__ import annotations` FastAPI
# resolves the endpoint's string annotations against module globals — a router-local class is
# invisible there and the body param degrades to a query param (social_api.py precedent).
if BaseModel is not None:

    class AnalyseReq(BaseModel):
        url: str = Field(min_length=8, max_length=2048)
        refresh: bool = False

    class CheckReq(BaseModel):
        # Force-check one "Not checked" claim (#397). The client echoes the claim back from its own
        # public shape; the server re-rates extremity and re-runs corroboration (never trusts the
        # client for the verdict). ``evidence_span`` isn't needed — this claim already passed the
        # span/injection guard when the article was analysed.
        url: str = Field(min_length=8, max_length=2048)
        text: str = Field(min_length=1, max_length=2000)
        voice: str = "own"
        speaker: str | None = Field(default=None, max_length=200)
        central: bool = False


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

        # Run the analysis DECOUPLED from the stream (#391): a client disconnect must NOT cancel it.
        # The LLM+search spend runs on a thread inside run_analysis and completes regardless — the
        # old `finally: task.cancel()` killed the coroutine at its await, so public_payload → cache
        # → analysis.completed never ran and a whole paid analysis was lost. Now the task runs to
        # completion and PERSISTS whether or not anyone is still listening; the module-level set is
        # the strong reference that keeps it from being GC'd. Concurrency is bounded by _SEM.
        task = asyncio.create_task(runner())
        _INFLIGHT.add(task)
        task.add_done_callback(_INFLIGHT.discard)

        async def stream():
            yield _sse("start", {"analysis_id": aid})
            while True:
                try:
                    kind, data = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    if task.done() and queue.empty():
                        return  # analysis finished with nothing left to drain — don't hang open
                    yield ": keep-alive\n\n"  # hold proxies open through slow phases
                    continue
                if kind in ("done", "error"):
                    yield _sse(kind, data)
                    return
                if kind == "scored":
                    continue  # the final payload carries the score
                yield _sse(kind, _public_progress(kind, data))

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/analyse/check-claim")
    async def check_claim_endpoint(req: CheckReq, request: Request):
        # On-demand force-check of ONE skipped claim (#397). Looser budget than a full analysis (one
        # web-search pass, no extraction), but still real spend — its own per-IP limiter.
        if not _CHECK_LIMITER.allow(client_ip(request.scope)):
            return JSONResponse(
                {"detail": "check rate limit exceeded — try again shortly"},
                status_code=429,
                headers={"Retry-After": str(_CHECK_LIMITER.retry_after())},
            )
        try:
            claim = await run_check(
                request.app.state, req.url, req.text,
                voice=req.voice, speaker=req.speaker, central=req.central,
            )
            return JSONResponse({"claim": claim})
        except AnalyseError as e:  # user-facing by contract
            return JSONResponse({"detail": str(e)}, status_code=400)
        except Exception:  # noqa: BLE001 - internal: log to the container, mask on the wire
            traceback.print_exc()
            return JSONResponse({"detail": "check failed — try again shortly"}, status_code=500)

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
