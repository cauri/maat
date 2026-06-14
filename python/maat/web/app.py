"""Maat reader — a minimal web feed over the Postgres projections.

Rolls corroborated facts up into stories (§5.7): each story leads with its most-asserted
claim, carries a gate-the-floor confidence label, and lists the other corroborated facts
beneath. Then every source article and the claims pulled from it. Run: `make web`.
"""

from __future__ import annotations

import html
import json
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from maat.pipeline.corroborate import confidence_label as _confidence_label

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
    label, tier = _confidence_label(conf)
    return (
        f'<div class="conf {tier}"><div class="cbar"><div class="cfill" style="width:{pct}%"></div></div>'
        f'<span class="cpct">{pct}%</span><span class="label {tier}">{html.escape(label)}</span></div>'
    )


def _headline(cl, id_to_source) -> str:
    rows = []
    for grp in _jload(cl["originators"]):
        names = sorted({id_to_source.get(a, a) for a in grp})
        wire = len(grp) > 1
        lbl = "wire · collapsed" if wire else "independent"
        rows.append(
            f'<div class="orig {"wire" if wire else "indep"}">'
            f'<span class="ol">{lbl}</span>{html.escape(", ".join(names))}</div>'
        )
    primary = _badge("primary source", "fact") if cl["has_primary"] else ""
    n_src = len(_jload(cl["sources"]))
    extremity = cl["extremity"] or "notable"
    ex_text = "extraordinary · bar raised" if extremity == "extraordinary" else f"{extremity} claim"
    ex_badge = f'<span class="ex {extremity}">{html.escape(ex_text)}</span>'
    return (
        f'<div class="cfact">{html.escape(cl["fact"])}</div>'
        f"{_conf_bar(cl)}"
        f'<div class="cmeta"><b>{n_src}</b> sources &rarr; '
        f'<b>{cl["independent_originators"]}</b> independent originators {ex_badge} {primary}</div>'
        f'<div class="origs">{"".join(rows)}</div>'
    )


def _supporting(cl) -> str:
    conf = float(cl["confidence"] or 0.0)
    pct = round(conf * 100)
    _, tier = _confidence_label(conf)
    return (
        f'<div class="sup"><span class="sup-pct {tier}">{pct}%</span>'
        f'<span class="sup-fact">{html.escape(cl["fact"])}</span></div>'
    )


def _story(group, id_to_source) -> str:
    head = _headline(group[0], id_to_source)
    sup = ""
    if len(group) > 1:
        items = "".join(_supporting(c) for c in group[1:])
        sup = f'<div class="sup-wrap"><div class="sup-head">Also corroborated in this story</div>{items}</div>'
    return f'<div class="story">{head}{sup}</div>'


def _page(articles, by_article, clusters, id_to_source) -> str:
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
    return (
        _HTML.replace("{{panel}}", panel)
        .replace("{{cards}}", cards)
        .replace("{{count}}", str(n_stories or len(articles)))
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
.story{padding:15px 0;border-top:1px solid var(--line)}
.story:first-of-type{border-top:0}
.cfact{font-weight:600;font-size:16px;letter-spacing:-.01em}
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
.foot{margin-top:10px;padding-top:8px;border-top:1px solid var(--line);font-size:12px;color:var(--mut)}
.muted,.empty{color:var(--mut)}
.empty{text-align:center;padding:60px 0}
</style></head><body>
<header class="top"><h1>Maat</h1><p>{{count}} stories · corroboration over spread · confidence on every claim</p></header>
<main>{{panel}}{{cards}}</main></body></html>"""
