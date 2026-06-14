"""Maat reader — a minimal web feed over the Postgres projections.

Shows the corroboration read (independent originators per fact, §5.5) up top, then every
article and the claims pulled from it with their veracity signals. Run: `make web`.
"""

from __future__ import annotations

import html
import json
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

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
