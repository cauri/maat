"""Maat reader — a minimal web feed over the Postgres projections.

Shows the corroboration read (independent originators per fact, §5.5) up top, then every
article and the claims pulled from it with their veracity signals. Run: `make web`.
"""

from __future__ import annotations

import html
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

DB = os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DB)
    yield
    await app.state.pool.close()


app = FastAPI(lifespan=lifespan, title="Maat reader")


@app.get("/", response_class=HTMLResponse)
async def feed() -> str:
    pool = app.state.pool
    articles = await pool.fetch(
        "select id, title, source, language from articles order by ingested_at desc"
    )
    claims = await pool.fetch(
        "select article_id, voice, speaker, kind, is_synthesis, horizon, in_headline, text "
        "from claims order by created_at"
    )
    clusters = await pool.fetch(
        "select fact, sources, originators, independent_originators, has_primary, confidence, extremity "
        "from clusters order by confidence desc, independent_originators desc"
    )
    id_to_source = {a["id"]: a["source"] for a in articles}
    by_article: dict[str, list] = {}
    for c in claims:
        by_article.setdefault(c["article_id"], []).append(c)
    return _page(articles, by_article, clusters, id_to_source)


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


def _story_json(cluster, claims_by_id: dict, meta: dict[str, dict]) -> dict:
    claim_ids = [str(x) for x in _jload(cluster["claim_ids"])]
    claims = [_claim_json(claims_by_id[cid], meta) for cid in claim_ids if cid in claims_by_id]
    languages = sorted({c["language"] for c in claims}) or ["en"]
    return {
        "id": cluster["id"],
        "fact": cluster["fact"],
        "confidence": float(cluster["confidence"] or 0.0),
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


def _badge(text: str, cls: str) -> str:
    return f'<span class="b {cls}">{html.escape(text)}</span>'


def _claim(c) -> str:
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
    return (
        f'<div class="claim"><div class="bs">{"".join(badges)}</div>'
        f'<div class="t">{html.escape(c["text"])}</div></div>'
    )


def _card(a, claims) -> str:
    rows = "".join(_claim(c) for c in claims) or '<div class="claim t muted">no claims</div>'
    return (
        f'<article class="card"><div class="src">{html.escape(a["source"] or "")}</div>'
        f'<h2>{html.escape(a["title"] or "")}</h2>'
        f'<div class="claims">{rows}</div>'
        f'<div class="foot">{len(claims)} claims</div></article>'
    )


def _jload(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _corro(cl, id_to_source) -> str:
    origs = _jload(cl["originators"])
    rows = []
    for grp in origs:
        names = sorted({id_to_source.get(a, a) for a in grp})
        wire = len(grp) > 1
        label = "wire · collapsed" if wire else "independent"
        rows.append(
            f'<div class="orig {"wire" if wire else "indep"}">'
            f'<span class="ol">{label}</span>{html.escape(", ".join(names))}</div>'
        )
    primary = _badge("primary source", "fact") if cl["has_primary"] else ""
    n_src = len(_jload(cl["sources"]))
    conf = float(cl["confidence"] or 0.0)
    pct = round(conf * 100)
    lvl = "hi" if conf >= 0.8 else "mid" if conf >= 0.5 else "lo"
    extremity = cl["extremity"] or "notable"
    ex_text = "extraordinary · bar raised" if extremity == "extraordinary" else f"{extremity} claim"
    ex_badge = f'<span class="ex {extremity}">{html.escape(ex_text)}</span>'
    return (
        f'<div class="corro"><div class="cfact">{html.escape(cl["fact"])}</div>'
        f'<div class="conf {lvl}"><div class="cbar"><div class="cfill" style="width:{pct}%"></div></div>'
        f'<span class="cpct">{pct}%</span><span class="clab">confidence</span></div>'
        f'<div class="cmeta"><b>{n_src}</b> sources &rarr; '
        f'<b>{cl["independent_originators"]}</b> independent originators {ex_badge} {primary}</div>'
        f'<div class="origs">{"".join(rows)}</div></div>'
    )


def _page(articles, by_article, clusters, id_to_source) -> str:
    panel = ""
    if clusters:
        items = "".join(_corro(c, id_to_source) for c in clusters)
        panel = f'<section class="panel"><h3>Corroboration · independent originators, not spread</h3>{items}</section>'
    cards = "".join(_card(a, by_article.get(a["id"], [])) for a in articles)
    if not cards:
        cards = '<p class="empty">No articles yet — start the agents and ingest a corpus.</p>'
    return (
        _HTML.replace("{{panel}}", panel)
        .replace("{{cards}}", cards)
        .replace("{{count}}", str(len(articles)))
    )


_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maat</title><style>
:root{--bg:#faf9f7;--card:#fff;--ink:#1c1b19;--mut:#7a7770;--line:#ece9e3}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header.top{padding:28px 20px 6px;max-width:760px;margin:0 auto}
header.top h1{margin:0;font-size:26px;letter-spacing:-.02em}
header.top p{margin:4px 0 0;color:var(--mut);font-size:14px}
main{max-width:760px;margin:0 auto;padding:12px 20px 60px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:14px 0}
.panel h3{margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
.corro{padding:11px 0;border-top:1px solid var(--line)}
.corro:first-of-type{border-top:0}
.cfact{font-weight:600;letter-spacing:-.01em}
.cmeta{font-size:14px;color:#3a3833;margin:2px 0 7px}
.conf{display:flex;align-items:center;gap:9px;margin:6px 0 7px}
.cbar{flex:1;height:7px;background:var(--line);border-radius:5px;overflow:hidden}
.cfill{height:100%;border-radius:5px}
.conf.hi .cfill{background:#3b6d11}.conf.mid .cfill{background:#92580a}.conf.lo .cfill{background:#b3402e}
.cpct{font-weight:700;font-size:14px;font-variant-numeric:tabular-nums}
.conf.hi .cpct{color:#3b6d11}.conf.mid .cpct{color:#92580a}.conf.lo .cpct{color:#b3402e}
.clab{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut)}
.ex{font-size:11px;font-weight:600;padding:1px 8px;border-radius:20px;background:#f0efe9;color:#67645d}
.ex.extraordinary{background:#fbe4df;color:#b3402e}
.origs{display:flex;flex-direction:column;gap:5px}
.orig{font-size:13px;padding:5px 11px;border-radius:9px}
.orig.wire{background:#faeeda}
.orig.indep{background:#eaf3de}
.ol{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin-right:8px}
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
.foot{margin-top:10px;padding-top:8px;border-top:1px solid var(--line);font-size:12px;color:var(--mut)}
.muted,.empty{color:var(--mut)}
.empty{text-align:center;padding:60px 0}
</style></head><body>
<header class="top"><h1>Maat</h1><p>{{count}} stories · corroboration over spread · confidence on every claim</p></header>
<main>{{panel}}{{cards}}</main></body></html>"""
