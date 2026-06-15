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

import json
from datetime import datetime, timezone
from typing import Any

from maat.agents.curation import Story as CurationStory, curate
from maat.pipeline.corroborate import confidence_label

# FastAPI is a hard dependency, but keep the import guarded so the pure builders above stay
# importable in any stripped-down env. Hoisted to module scope (not deferred inside _make_router)
# so the route parameter/return annotations resolve for OpenAPI — `from __future__ import
# annotations` makes every annotation a forward-ref, which needs the names in module globals.
try:  # pragma: no cover - exercised whenever FastAPI is present (the normal case)
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - FastAPI absent (pure-builder-only env)
    APIRouter = HTTPException = Request = JSONResponse = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        "claims": claims,
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

        country = _infer_country(
            story["claims"], article_meta, cluster.get("originators")
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
# Thin FastAPI router (reads DB → calls pure builders — no app.py edits)
# ---------------------------------------------------------------------------


def _make_router() -> Any:
    """Build and return the APIRouter.  FastAPI is imported at module scope (guarded) so the
    route annotations resolve for OpenAPI; this only runs when FastAPI is present."""
    router = APIRouter(prefix="/api/v2", tags=["feed-v2"])

    async def _load_article_meta(pool) -> dict[str, dict[str, Any]]:
        rows = await pool.fetch(
            "select id, source, language, title, url from articles"
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

    @router.get("/feed", response_class=JSONResponse)
    async def feed_endpoint(request: Request):
        """Served feed: stories ordered by confidence then de-US re-ranked."""
        pool = request.app.state.pool
        clusters = await _load_clusters(pool)
        article_meta = await _load_article_meta(pool)
        claims_by_id = await _load_claims_by_id(pool)
        payload = build_feed(clusters, claims_by_id, article_meta)
        return JSONResponse(payload)

    @router.get("/story/{cluster_id}", response_class=JSONResponse)
    async def story_endpoint(cluster_id: str, request: Request):
        """Single story detail including full article texts."""
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

        # Attach full article texts the Apple reader opens
        article_ids = list({c["article_id"] for c in story["claims"] if c.get("article_id")})
        if article_ids:
            full_rows = await pool.fetch(
                "select id, source, title, url, language, body, ingested_at from articles "
                "where id = any($1::text[])",
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
                    "ingested_at": (
                        r["ingested_at"].isoformat() if r.get("ingested_at") else None
                    ),
                }
                for r in full_rows
            ]
        else:
            story["articles"] = []

        return JSONResponse(story)

    return router


# Module-level router — mount with: app.include_router(feed_router)
try:
    feed_router = _make_router()
except Exception:  # pragma: no cover — FastAPI may not be installed in test env
    feed_router = None  # type: ignore[assignment]
