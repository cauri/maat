"""Maat reader — a minimal web feed over the Postgres projections.

Shows every article and the claims the pipeline pulled from it, with the veracity signals
we have so far (voice/attribution, fact vs projection, synthesis). Corroboration + a
confidence read fill in as §5.4-5.7 land. Run: `make web` (or uvicorn maat.web.app:app).
"""

from __future__ import annotations

import html
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
    by_article: dict[str, list] = {}
    for c in claims:
        by_article.setdefault(c["article_id"], []).append(c)
    return _page(articles, by_article)


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
        f'<div class="foot">{len(claims)} claims · corroboration pending</div></article>'
    )


def _page(articles, by_article) -> str:
    cards = "".join(_card(a, by_article.get(a["id"], [])) for a in articles)
    if not cards:
        cards = '<p class="empty">No articles yet — start the agents and ingest a corpus.</p>'
    return _HTML.replace("{{cards}}", cards).replace("{{count}}", str(len(articles)))


_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maat</title><style>
:root{--bg:#faf9f7;--card:#fff;--ink:#1c1b19;--mut:#7a7770;--line:#ece9e3}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header.top{padding:28px 20px 8px;max-width:760px;margin:0 auto}
header.top h1{margin:0;font-size:26px;letter-spacing:-.02em}
header.top p{margin:4px 0 0;color:var(--mut);font-size:14px}
main{max-width:760px;margin:0 auto;padding:12px 20px 60px}
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
<main>{{cards}}</main></body></html>"""
