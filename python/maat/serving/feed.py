"""Feed API (P5/P6, issue #48) — pure builders + thin FastAPI router.

The Apple client consumes /api/v2/feed and /api/v2/story/{id}.  This module
provides two layers:

  1. PURE BUILDER FUNCTIONS (no I/O, no DB, fully testable):
       build_claim()             — one claim row → JSON-able dict
       build_originator_groups() — originator column → provenance groups
       build_story()             — one cluster row + claims + article meta → JSON-able dict
       build_feed()              — list of cluster rows → full feed payload with
                                   confidence labels, provenance, and de-US ordering

  2. THIN FASTAPI ROUTER (reads projections, calls pure builders — no app.py edits):
       GET /api/v2/feed
       GET /api/v2/story/{cluster_id}

The DB queries read the same projections as the admin console (app.py) — articles,
claims, clusters — and pass rows in to the pure builders.  No schema changes, no
new tables.

Veracity contract:
- confidence_label() (§5.7) maps conf → (verdict_text, tier_code)
- curate() (curation.py) applies de-US re-ranking; confidence values are immutable
- The payload shape mirrors the Swift `Story` model in the Apple client
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from maat.agents.curation import Story as CurationStory, curate
from maat.events import STORY_GEO_INFERRED
from maat.learning.accuracy import lifecycle_by_fact
from maat.learning.reputation import fold_reputation
from maat.learning.source_learning import learn_preferences
from maat.pipeline.corroborate import confidence_label, is_primary_source
from maat.serving.source_flags import denied_sources
from maat.serving.topics import enriched_interest, parse_interest, story_matches

# FastAPI is a hard dependency, but keep the import guarded so the pure builders above stay
# importable in any stripped-down env. Hoisted to module scope (not deferred inside _make_router)
# so the route parameter/return annotations resolve for OpenAPI — `from __future__ import
# annotations` makes every annotation a forward-ref, which needs the names in module globals.
try:  # pragma: no cover - exercised whenever FastAPI is present (the normal case)
    from fastapi import APIRouter, HTTPException, Request, Response
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - FastAPI absent (pure-builder-only env)
    APIRouter = HTTPException = Request = Response = JSONResponse = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_by_topics(payload: dict, topics: str, *, use_llm: bool = False) -> dict:
    """Personal-feed filter (#50): keep only stories matching the reader's NL interests.

    ``topics`` is a comma-separated free-text list ("art, West African politics"); each is parsed
    to a ``TopicSpec`` and matched against the story's fact + claim texts via ``story_matches``.
    No topics → the payload is returned unchanged, so the default feed and every existing client
    are untouched. ``use_llm`` (#189) swaps the pure keyword parse for the memoised bulk-model
    enrichment (``enriched_interest``) — same matching, broader recall; the LLM call is cached per
    interest so this stays cheap on the hot path.
    """
    wanted = [t.strip() for t in (topics or "").split(",") if t.strip()]
    if not wanted:
        return payload
    specs = [enriched_interest(t) if use_llm else parse_interest(t) for t in wanted]
    kept = [
        s
        for s in payload.get("stories", [])
        if story_matches(
            {
                "title": s.get("fact", ""),
                "body": " ".join(c.get("text", "") for c in s.get("claims", [])),
            },
            specs,
        )
    ]
    return {**payload, "stories": kept, "count": len(kept)}


def _filter_denied(payload: dict, denied: set) -> dict:
    """Operator source-deny enforcement (#187): drop stories sourced ENTIRELY from denied sources.
    A story with at least one non-denied source stays — its corroboration still stands."""
    if not denied:
        return payload
    kept = []
    for s in payload.get("stories", []):
        srcs = {src for g in s.get("originator_groups", []) for src in g.get("sources", [])}
        if srcs and srcs <= denied:
            continue
        kept.append(s)
    return {**payload, "stories": kept, "count": len(kept)}


def _thread_payload(
    payload: dict,
    cluster_node: dict[str, str],
    node_meta: dict[str, dict],
    node_edges: dict[str, list],
) -> dict:
    """Attach story-graph threading (#42/#44) to a feed payload: tag each story with its
    event-node, and add a top-level ``threads`` list grouping the clusters that belong to one
    developing story, with their typed develops/spawns/merges edges. Additive — a client that
    ignores ``threads`` / ``node_id`` still gets the flat feed.
    """
    stories = payload.get("stories", [])
    for s in stories:
        nid = cluster_node.get(s.get("id"))
        if nid:
            s["node_id"] = nid
            s["node_headline"] = (node_meta.get(nid) or {}).get("headline")
    threads: list[dict] = []
    seen: set[str] = set()
    for s in stories:
        nid = s.get("node_id")
        if not nid or nid in seen:
            continue
        seen.add(nid)
        members = [t.get("id") for t in stories if t.get("node_id") == nid]
        if len(members) < 2:
            continue  # a single-cluster node isn't a thread worth surfacing
        threads.append(
            {
                "node_id": nid,
                "headline": (node_meta.get(nid) or {}).get("headline"),
                "cluster_ids": members,
                "edges": node_edges.get(nid, []),
            }
        )
    return {**payload, "threads": threads}


def _annotate_accuracy(payload: dict, lifecycle: dict) -> dict:
    """Tag each story with its accuracy-axis lifecycle state (#38) — how the fact has resolved
    over time (dormant/resolving/resolved/extended/decayed), folded from the cluster.corroborated
    history by ``maat.learning.accuracy.lifecycle_by_fact``. Additive; facts with no history stay
    unannotated. Opt-in (``?accuracy=1``) so the default feed isn't slowed by the history fold.
    """
    for s in payload.get("stories", []):
        fact = " ".join((s.get("fact") or "").lower().split())  # same normalisation as the fold
        state = lifecycle.get(fact)
        if state is not None:
            s["accuracy_state"] = getattr(state, "value", state)
    return payload


def _reputation_map(reps) -> dict:
    """Per-source reputation (the §6 truthfulness-over-time fold) as {source: reputation} for the
    feed — surfacing the learned reputation into the PRODUCT (#199), not just the operator console.
    """
    return {r.source: round(r.reputation, 4) for r in reps}


def _preferences_payload(prefs) -> dict:
    """Serialise learned acquisition preferences (#35) for /api/v2/source-preferences — which
    sources have proven reliable over time, ranked by acquisition weight (diversity-floored)."""
    return {
        "ranked": [
            {
                "source": p.source,
                "rank": p.rank,
                "acquisition_weight": round(p.acquisition_weight, 4),
                "confirmation_rate": p.confirmation_rate,
                "independent_rate": round(p.independent_rate, 4),
                "in_diversity_floor": p.in_diversity_floor,
                "low_evidence": p.low_evidence,
            }
            for p in prefs.ranked
        ],
        "diversity_floor": sorted(prefs.diversity_floor),
        "note": "Learned acquisition preferences (#35): which sources have proven reliable, "
        "computed read-time from the corroboration history. ACTUATION — biasing acquisition "
        "toward these within the diversity floor — is a flagged policy decision, not yet enforced.",
    }


def _jload(v: Any) -> list:
    """Parse a JSON column that may already be decoded (asyncpg returns Python objects)."""
    if isinstance(v, str):
        return json.loads(v)
    if v is None:
        return []
    return list(v)


# ---------------------------------------------------------------------------
# Pure builder: single claim
# ---------------------------------------------------------------------------


def build_claim(
    claim: dict[str, Any],
    article_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a JSON-able claim dict from a claim projection row and article metadata.

    Parameters
    ----------
    claim:
        A dict representing one row from the ``claims`` projection.  Expected keys:
        id, article_id, text, voice, speaker, kind, is_synthesis, horizon,
        in_headline, evidence_span.  Unknown keys are silently ignored.
    article_meta:
        Map of article_id → article row dict.  Used to attach source and language.

    Returns
    -------
    A JSON-serialisable dict the Apple client can render directly.
    """
    aid = claim.get("article_id") or ""
    art = article_meta.get(aid) or {}
    return {
        "id": str(claim.get("id") or ""),
        "text": claim.get("text") or "",
        "voice": claim.get("voice"),
        "speaker": claim.get("speaker"),
        "kind": claim.get("kind"),
        "is_synthesis": bool(claim.get("is_synthesis")),
        "horizon": claim.get("horizon"),
        "in_headline": bool(claim.get("in_headline")),
        "evidence_span": claim.get("evidence_span"),
        "article_id": aid,
        "source": art.get("source"),
        "language": art.get("language") or "en",
    }


# ---------------------------------------------------------------------------
# Pure builder: originator provenance groups
# ---------------------------------------------------------------------------


def build_originator_groups(
    originators_raw: Any,
    article_meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand the originators JSON column into human-readable groups.

    Each originator group is a list of article ids collapsed to one independent
    originator (§5.5: wire reprints or citation cascades).  We map article ids
    → source names for the client.

    Parameters
    ----------
    originators_raw:
        The ``originators`` column value (list of lists of article-id strings),
        already decoded by asyncpg or still a JSON string.
    article_meta:
        Map of article_id → article row dict.

    Returns
    -------
    List of dicts: ``{"sources": [str, ...], "collapsed": bool}``.
    A collapsed group (len > 1) represents wire-syndicated or cascade articles.
    """
    groups = []
    for grp in _jload(originators_raw):
        sources = sorted({(article_meta.get(a) or {}).get("source") or a for a in grp})
        groups.append({"sources": sources, "collapsed": len(grp) > 1})
    return groups


# ---------------------------------------------------------------------------
# Pure builder: single story
# ---------------------------------------------------------------------------


def build_story(
    cluster: dict[str, Any],
    claims_by_id: dict[str, dict[str, Any]],
    article_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Assemble a JSON-able story dict from a cluster row + supporting projections.

    A *story* in the served feed is one corroboration cluster (§5.5): a set of
    same-fact claims that have been independently corroborated.  The confidence
    read (§5.6) and label (§5.7) are derived here from the stored projection
    values — no LLM, no recomputation.

    Parameters
    ----------
    cluster:
        Dict representing one row from the ``clusters`` projection.  Expected keys:
        id, fact, sources, originators, independent_originators, has_primary,
        claim_ids, confidence, extremity.
    claims_by_id:
        Map of claim_id (str) → claim row dict.
    article_meta:
        Map of article_id → article row dict.

    Returns
    -------
    JSON-serialisable dict with:
    - id, fact, confidence, verdict (label text), tier (colour code),
      extremity, independent_originators, has_primary,
      source_count, originator_groups (provenance),
      languages, claims (list of claim dicts).
    """
    conf = float(cluster.get("confidence") or 0.0)
    ind = int(cluster.get("independent_originators") or 0)
    has_primary = bool(cluster.get("has_primary"))
    extremity = cluster.get("extremity") or "notable"

    verdict, tier = confidence_label(
        conf,
        independent_originators=ind,
        has_primary=has_primary,
        extremity=extremity,
    )

    claim_ids = [str(x) for x in _jload(cluster.get("claim_ids"))]
    claims = [
        build_claim(claims_by_id[cid], article_meta)
        for cid in claim_ids
        if cid in claims_by_id
    ]

    languages = sorted({c["language"] for c in claims if c.get("language")}) or ["en"]

    return {
        "id": cluster.get("id") or "",
        "fact": cluster.get("fact") or "",
        "confidence": round(conf, 4),
        "verdict": verdict,
        "tier": tier,
        "extremity": extremity,
        "independent_originators": ind,
        "has_primary": has_primary,
        "source_count": len(_jload(cluster.get("sources"))),
        "originator_groups": build_originator_groups(
            cluster.get("originators"), article_meta
        ),
        "languages": languages,
        "hero_image_article_id": _hero_image_article_id(cluster, claims, article_meta),
        "claims": claims,
    }


def build_deeper(
    cluster: dict[str, Any],
    claims: list[dict[str, Any]],
    article_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Tier-3 'go deeper' expanded provenance (#56) — computed server-side from the existing
    projections (no new fetch yet): a per-originator breakdown with language + primary flags, the
    cross-language source spread, and the explicit primary-source list. Replaces the Apple client's
    fabricated ``synthesizeDeeper`` stub with real provenance. Primary-source FETCH + cross-language
    re-verification (the deepest tier) remains a follow-up.
    """
    src_lang: dict[str, str] = {}
    for c in claims:
        if c.get("source"):
            src_lang[c["source"]] = c.get("language") or "en"
    originators = []
    for grp in build_originator_groups(cluster.get("originators"), article_meta):
        srcs = grp["sources"]
        originators.append(
            {
                "sources": srcs,
                "collapsed": grp["collapsed"],
                "languages": sorted({src_lang.get(s, "en") for s in srcs}),
                "has_primary": any(is_primary_source(s) for s in srcs),
            }
        )
    by_lang: dict[str, set] = {}
    for c in claims:
        if c.get("source"):
            by_lang.setdefault(c.get("language") or "en", set()).add(c["source"])
    languages = [{"language": k, "sources": sorted(v)} for k, v in sorted(by_lang.items())]
    all_sources = sorted({c.get("source") for c in claims if c.get("source")})
    return {
        "originators": originators,
        "languages": languages,
        "primary_sources": [s for s in all_sources if is_primary_source(s)],
        "source_count": len(all_sources),
        "note": "Server-computed expanded provenance (Tier-3, #56). "
        "Primary-source fetch + cross-language re-verification is the next tier.",
    }


# ---------------------------------------------------------------------------
# Geography inference helpers (best-effort, no LLM — for de-US ranking only)
# ---------------------------------------------------------------------------

# Language → most-probable country (rough, for de-US balancing only — not truth claims)
_LANG_TO_COUNTRY: dict[str, str] = {
    "ar": "SA", "de": "DE", "es": "ES", "fr": "FR", "hi": "IN", "it": "IT",
    "ja": "JP", "ko": "KR", "nl": "NL", "pl": "PL", "pt": "BR", "ru": "RU",
    "sv": "SE", "tr": "TR", "uk": "UA", "zh": "CN",
}

# TLD → country (for bare-domain sources like "bbc.co.uk", "lemonde.fr")
_TLD_TO_COUNTRY: dict[str, str] = {
    "co.uk": "GB", "uk": "GB", "fr": "FR", "de": "DE", "it": "IT", "es": "ES",
    "nl": "NL", "pt": "PT", "com.br": "BR", "br": "BR", "au": "AU", "ca": "CA",
    "cn": "CN", "jp": "JP", "kr": "KR", "ru": "RU", "co.in": "IN", "in": "IN",
    "co.za": "ZA", "za": "ZA", "ng": "NG", "ke": "KE", "eg": "EG",
    "ar": "AR", "mx": "MX", "tr": "TR", "se": "SE", "no": "NO", "pl": "PL",
}


def _source_country(source: str) -> str:
    """Guess ISO-3166-1 alpha-2 country from a source domain. Empty string if unknown."""
    s = (source or "").lower().strip()
    if not s:
        return ""
    # Longest-match TLD suffix first
    for tld, country in sorted(_TLD_TO_COUNTRY.items(), key=lambda x: -len(x[0])):
        if s.endswith("." + tld) or s == tld:
            return country
    return ""


def _infer_country(
    claims: list[dict[str, Any]],
    article_meta: dict[str, dict[str, Any]],
    originators_raw: Any,
) -> str:
    """Best-effort country inference for curation — not a veracity signal.

    Order of preference:
    1. Source-domain TLD from independent originator articles.
    2. Language of the claims (non-English only — English doesn't narrow to a country).
    Falls back to "" (unknown) — curate() treats unknown country as uncapped.
    """
    for grp in _jload(originators_raw):
        for aid in grp:
            art = article_meta.get(aid) or {}
            c = _source_country(art.get("source") or "")
            if c:
                return c
    for claim in claims:
        lang = (claim.get("language") or "").lower()[:2]
        if lang and lang != "en":
            c = _LANG_TO_COUNTRY.get(lang, "")
            if c:
                return c
    return ""


def _resolve_country(
    claims: list[dict[str, Any]],
    article_meta: dict[str, dict[str, Any]],
    originators_raw: Any,
    cluster_id: str,
    geo_overrides: dict[str, str] | None,
) -> str:
    """Country for curation: the TLD/language heuristic first, then the LLM geo-tagger's
    inference (#189) only for the gap the heuristic left blank. The heuristic stays
    authoritative — the override never replaces a country the heuristic could place."""
    country = _infer_country(claims, article_meta, originators_raw)
    if not country and geo_overrides:
        country = geo_overrides.get(cluster_id, "")
    return country


def _primary_source(
    cluster: dict[str, Any],
    article_meta: dict[str, dict[str, Any]],
) -> str:
    """Return the most prominent source name from the first originator group."""
    for grp in _jload(cluster.get("originators")):
        for aid in grp:
            src = (article_meta.get(aid) or {}).get("source") or ""
            if src:
                return src
    sources = _jload(cluster.get("sources"))
    return sources[0] if sources else ""


def _hero_image_article_id(
    cluster: dict[str, Any],
    claims: list[dict[str, Any]],
    article_meta: dict[str, dict[str, Any]],
) -> str | None:
    """Article id whose lead image best represents the story (for the client's proxy URL).

    Prefer the primary originator's article; fall back to any claim's article with an image.
    Returns the article id — never the raw URL — because the client fetches the image through
    the reader's proxy (/api/v2/image?article=<id>), so the origin server never sees the
    reader's users (privacy, #1). Display-only; never a veracity signal.
    """
    for grp in _jload(cluster.get("originators")):
        for aid in grp:
            if (article_meta.get(aid) or {}).get("image_url"):
                return aid
    for claim in claims:
        aid = claim.get("article_id")
        if aid and (article_meta.get(aid) or {}).get("image_url"):
            return aid
    return None


# ---------------------------------------------------------------------------
# Pure builder: full feed
# ---------------------------------------------------------------------------


def build_feed(
    clusters: list[dict[str, Any]],
    claims_by_id: dict[str, dict[str, Any]],
    article_meta: dict[str, dict[str, Any]],
    *,
    country_cap: float = 0.25,
    source_cap: float = 0.20,
    confidence_band: float = 0.20,
    geo_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Assemble the full feed payload: stories + de-US ordering.

    This is the root builder the router calls.  It:
      1. Builds a story dict for each cluster (confidence label, provenance, claims).
      2. Wraps each story as a CurationStory to drive the de-US re-ranker.
      3. Applies curate() to reorder for geographic/source diversity.
      4. Returns a JSON-able envelope the Apple client deserialises.

    The de-US ranking only shuffles order; it never alters confidence values or
    veracity labels.

    Parameters
    ----------
    clusters:
        Rows from ``clusters`` projection, pre-ordered (typically
        ``confidence desc, independent_originators desc``).
    claims_by_id:
        Map of claim_id (str) → claim row dict.
    article_meta:
        Map of article_id → article row dict.
    country_cap, source_cap, confidence_band:
        Knobs forwarded to curate(); see curation.py for semantics.

    Returns
    -------
    Dict with keys: generated_at, count, stories.
    """
    if not clusters:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": 0,
            "stories": [],
        }

    stories_by_id: dict[str, dict[str, Any]] = {}
    curation_inputs: list[CurationStory] = []

    for cluster in clusters:
        story = build_story(cluster, claims_by_id, article_meta)
        sid = story["id"]
        stories_by_id[sid] = story

        # #189: heuristic first, then the LLM geo-tagger fills only the gaps it left blank —
        # a de-US ordering hint, never a veracity signal.
        country = _resolve_country(
            story["claims"], article_meta, cluster.get("originators"), sid, geo_overrides
        )
        source = _primary_source(cluster, article_meta)

        curation_inputs.append(
            CurationStory(
                id=sid,
                confidence=story["confidence"],
                country=country,
                source=source,
                language=story["languages"][0] if story["languages"] else "en",
            )
        )

    ordered = curate(
        curation_inputs,
        country_cap=country_cap,
        source_cap=source_cap,
        confidence_band=confidence_band,
    )

    ordered_stories = [stories_by_id[s.id] for s in ordered if s.id in stories_by_id]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(ordered_stories),
        "stories": ordered_stories,
    }


# ---------------------------------------------------------------------------
# Image proxy (#1) — privacy-preserving, SSRF-guarded
# ---------------------------------------------------------------------------
#
# The Apple client requests article images by ARTICLE ID, never by URL: the reader looks up the
# stored og:image for that id and fetches it, so the origin server never sees the user's IP and a
# client cannot make the reader fetch an arbitrary host. The stored URL is still SSRF-guarded
# (public IPs only), size/time-capped, and the bytes are cached in-process. Display-only — image
# fetch outcomes never feed veracity.

_IMAGE_TIMEOUT = 6.0
_IMAGE_MAX_BYTES = 8 * 1024 * 1024  # 8 MB — generous for a hero image, bounds memory/abuse
_IMAGE_CACHE_MAX = 256  # FIFO cap; per-process, lossy across workers (fine — it's a cache)
_image_cache: dict[str, tuple[bytes, str]] = {}


async def _host_is_public(host: str, port: int) -> bool:
    """True only if EVERY resolved address for ``host`` is a public, routable IP.

    Blocks the obvious SSRF targets — loopback, RFC-1918 private ranges, link-local (incl. the
    169.254.169.254 cloud-metadata endpoint), reserved/multicast/unspecified. Residual gap: a
    DNS-rebind between this check and httpx's own resolution; acceptable for a low-value image
    proxy whose inputs are og:image tags from real news sites (defense-in-depth, not a vault).
    """
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
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


async def _safe_image_fetch(url: str) -> tuple[bytes, str] | None:
    """Fetch an image URL with SSRF + size + content-type guards. Returns (bytes, content_type).

    Redirects are followed manually (max 3 hops) so each hop's host is re-validated — httpx's
    auto-redirect would bypass the per-hop check. Anything non-image, oversized, or non-200
    returns None (the route answers 502).
    """
    current = url
    async with httpx.AsyncClient(follow_redirects=False, timeout=_IMAGE_TIMEOUT) as client:
        for _ in range(4):  # initial request + up to 3 redirects
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return None
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if not await _host_is_public(parsed.hostname, port):
                return None
            try:
                resp = await client.get(current, headers={"User-Agent": "maat-image-proxy/1"})
            except httpx.HTTPError:
                return None
            if resp.is_redirect:
                loc = resp.headers.get("location")
                if not loc:
                    return None
                current = urljoin(current, loc)
                continue
            if resp.status_code != 200:
                return None
            ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                return None
            data = resp.content
            if not data or len(data) > _IMAGE_MAX_BYTES:
                return None
            return data, ctype
    return None  # too many redirects


# ---------------------------------------------------------------------------
# Thin FastAPI router (reads DB → calls pure builders — no app.py edits)
# ---------------------------------------------------------------------------


def _make_router() -> Any:
    """Build and return the APIRouter.  FastAPI is imported at module scope (guarded) so the
    route annotations resolve for OpenAPI; this only runs when FastAPI is present."""
    router = APIRouter(prefix="/api/v2", tags=["feed-v2"])

    async def _load_article_meta(pool) -> dict[str, dict[str, Any]]:
        rows = await pool.fetch(
            "select id, source, language, title, url, image_url from articles"
        )
        return {r["id"]: dict(r) for r in rows}

    async def _load_claims_by_id(pool) -> dict[str, dict[str, Any]]:
        rows = await pool.fetch(
            "select id, article_id, voice, speaker, kind, is_synthesis, "
            "horizon, in_headline, evidence_span, text from claims"
        )
        return {str(r["id"]): dict(r) for r in rows}

    async def _load_clusters(pool) -> list[dict[str, Any]]:
        rows = await pool.fetch(
            "select id, fact, sources, originators, independent_originators, "
            "has_primary, claim_ids, confidence, extremity from clusters "
            "order by confidence desc, independent_originators desc"
        )
        return [dict(r) for r in rows]

    async def _load_story_graph(pool):
        """Story-graph projection (#42/#44) for threading. Resilient: if the tables haven't been
        migrated yet, returns empties so the feed degrades to flat (un-threaded)."""
        try:
            ncs = await pool.fetch("select node_id, cluster_id from story_node_clusters")
            nodes = await pool.fetch("select id, headline from story_nodes")
            edges = await pool.fetch("select kind, from_id, to_id from story_edges")
        except Exception:
            return {}, {}, {}
        cluster_node = {r["cluster_id"]: r["node_id"] for r in ncs}
        node_meta = {r["id"]: {"headline": r["headline"]} for r in nodes}
        node_edges: dict[str, list] = {}
        for e in edges:
            node_edges.setdefault(e["from_id"], []).append({"kind": e["kind"], "to": e["to_id"]})
        return cluster_node, node_meta, node_edges

    async def _load_corroboration_history(pool):
        """The cluster.corroborated event stream (oldest→newest) — the trajectory accuracy folds
        (#38). Resilient if the events table is unavailable."""
        try:
            rows = await pool.fetch(
                "select data from events where type = 'cluster.corroborated' order by id"
            )
        except Exception:
            return []
        return [json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in rows]

    async def _load_denied(pool):
        """Currently operator-denied sources (#187), folded from admin.source.flagged. Resilient."""
        try:
            rows = await pool.fetch(
                "select data from events where type = 'admin.source.flagged' order by id"
            )
        except Exception:
            return set()
        return denied_sources(
            (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]) for r in rows
        )

    async def _load_geo_overrides(pool):
        """LLM-inferred {cluster_id: country} from the geo-tagger (#189), latest per cluster.
        A de-US ordering hint that fills the heuristic's gaps — never a veracity signal. Resilient
        (the table is the generic events log; missing/empty → no overrides → pure heuristic)."""
        try:
            rows = await pool.fetch(
                "select stream_id, data from events where type = $1 order by id",
                STORY_GEO_INFERRED,
            )
        except Exception:
            return {}
        out: dict[str, str] = {}
        for r in rows:
            d = json.loads(r["data"]) if isinstance(r["data"], str) else r["data"]
            code = (d or {}).get("country") or ""
            if code:
                out[r["stream_id"]] = code  # later events win (latest per cluster)
        return out

    @router.get("/feed", response_class=JSONResponse)
    async def feed_endpoint(
        request: Request, topics: str = "", accuracy: int = 0, reputation: int = 0
    ):
        """Served feed: stories ordered by confidence then de-US re-ranked.

        ``?topics=`` (comma-separated NL interests) personalises the feed (#50).
        ``?accuracy=1`` tags each story with its accuracy-axis lifecycle state (#38).
        ``?reputation=1`` adds a {source: reputation} map (#199). Omitted → the full,
        un-annotated feed (backward-compatible)."""
        pool = request.app.state.pool
        clusters = await _load_clusters(pool)
        article_meta = await _load_article_meta(pool)
        claims_by_id = await _load_claims_by_id(pool)
        payload = build_feed(
            clusters, claims_by_id, article_meta, geo_overrides=await _load_geo_overrides(pool)
        )
        if topics.strip() and os.environ.get("MAAT_TOPICS_LLM") == "1":
            # #189: enrich interests with the bulk model, but off the event loop — a cache MISS
            # makes a blocking call; cache HITs (steady state) are instant. Pure path stays inline.
            payload = await asyncio.to_thread(_filter_by_topics, payload, topics, use_llm=True)
        else:
            payload = _filter_by_topics(payload, topics)
        payload = _filter_denied(payload, await _load_denied(pool))  # #187: drop denied-only stories
        cluster_node, node_meta, node_edges = await _load_story_graph(pool)
        payload = _thread_payload(payload, cluster_node, node_meta, node_edges)
        if accuracy or reputation:
            history = await _load_corroboration_history(pool)
            if history and accuracy:
                payload = _annotate_accuracy(
                    payload, lifecycle_by_fact(history, datetime.now(timezone.utc))
                )
            if history and reputation:
                payload["source_reputation"] = _reputation_map(fold_reputation(history))
        return JSONResponse(payload)

    @router.get("/source-preferences", response_class=JSONResponse)
    async def source_preferences_endpoint(request: Request):
        """Learned acquisition preferences (#35): fold the corroboration history into per-source
        reputation, then rank sources by learned acquisition weight (diversity-floored). Wires the
        previously-orphaned learn_preferences into a live read; acquisition actuation is flagged."""
        pool = request.app.state.pool
        history = await _load_corroboration_history(pool)
        prefs = learn_preferences(fold_reputation(history))
        return JSONResponse(_preferences_payload(prefs))

    @router.get("/story/{cluster_id}", response_class=JSONResponse)
    async def story_endpoint(cluster_id: str, request: Request, deeper: int = 0):
        """Single story detail including full article texts. ``?deeper=1`` adds a Tier-3
        expanded-provenance block (#56)."""
        pool = request.app.state.pool
        row = await pool.fetchrow(
            "select id, fact, sources, originators, independent_originators, "
            "has_primary, claim_ids, confidence, extremity from clusters where id = $1",
            cluster_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="no such story")

        article_meta = await _load_article_meta(pool)
        claims_by_id = await _load_claims_by_id(pool)

        story = build_story(dict(row), claims_by_id, article_meta)
        if deeper:  # Tier-3 "go deeper" (#56): server-computed expanded provenance
            story["deeper"] = build_deeper(dict(row), story["claims"], article_meta)

        # Attach full article texts the Apple reader opens
        article_ids = list({c["article_id"] for c in story["claims"] if c.get("article_id")})
        if article_ids:
            full_rows = await pool.fetch(
                "select id, source, title, url, language, body, image_url, ingested_at "
                "from articles where id = any($1::text[])",
                article_ids,
            )
            story["articles"] = [
                {
                    "id": r["id"],
                    "source": r.get("source"),
                    "title": r.get("title"),
                    "body": r.get("body") or "",
                    "url": r.get("url"),
                    "language": r.get("language") or "en",
                    # Raw og:image for transparency; the client still loads it via the proxy
                    # (/api/v2/image?article=<id>), never directly (privacy, #1).
                    "image_url": r.get("image_url"),
                    "ingested_at": (
                        r["ingested_at"].isoformat() if r.get("ingested_at") else None
                    ),
                }
                for r in full_rows
            ]
        else:
            story["articles"] = []

        return JSONResponse(story)

    @router.get("/image")
    async def image_proxy(article: str, request: Request):
        """Privacy-preserving image proxy (#1): client passes an ARTICLE ID, not a URL.

        We look up that article's stored og:image and stream it back, SSRF-guarded and cached,
        so the origin server never sees the reader's users and the client can't drive the reader
        to fetch arbitrary hosts. Display-only enrichment; never a veracity signal.
        """
        pool = request.app.state.pool
        row = await pool.fetchrow("select image_url from articles where id = $1", article)
        if row is None or not row["image_url"]:
            raise HTTPException(status_code=404, detail="no image for article")

        cached = _image_cache.get(article)
        if cached is None:
            cached = await _safe_image_fetch(row["image_url"])
            if cached is None:
                raise HTTPException(status_code=502, detail="image unavailable")
            if len(_image_cache) >= _IMAGE_CACHE_MAX:
                _image_cache.pop(next(iter(_image_cache)))  # FIFO eviction
            _image_cache[article] = cached

        data, ctype = cached
        return Response(
            content=data,
            media_type=ctype,
            headers={"Cache-Control": "public, max-age=86400"},  # let Caddy + client cache too
        )

    return router


# Module-level router — mount with: app.include_router(feed_router)
try:
    feed_router = _make_router()
except Exception:  # pragma: no cover — FastAPI may not be installed in test env
    feed_router = None  # type: ignore[assignment]
