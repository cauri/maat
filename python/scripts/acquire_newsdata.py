"""NewsData.io acquisition driver — a dedicated, reliable, multipolar news channel.

No-op unless ``MAAT_NEWSDATA_KEY`` is set. For the tracked topics, pull the latest articles from
NewsData.io once broadly and once per target language (a deliberate multipolar floor, like the
per-locale GDELT/Apify sweep #239), gate each candidate to a credible publisher, and ingest the
ones with real bodies tagged ``provider: newsdata`` with their detected language + country.

Run:  uv run python scripts/acquire_newsdata.py        (or `make acquire-newsdata`)
Env:  MAAT_NEWSDATA_KEY (required)      — the API key (operator-set in the box .env).
      MAAT_NEWSDATA_LANGS               — ISO-2 languages for the floor (default: a multipolar set).
      MAAT_NEWSDATA_MAXRECORDS (def 10) — records per query.
      Topics come from MAAT_TOPICS / config/topics.txt (same as the clock).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat import prompts
from maat.acquire import newsdata
from maat.acquire.source_gate import accept_source
from maat.bus import connect
from maat.events import publish
from maat.serving.source_flags import denied_sources

ROOT = Path(__file__).resolve().parents[2]

# A multipolar default — the languages an Anglophone-default stream under-covers (ISO-639-1, as
# NewsData expects). Operator-overridable via MAAT_NEWSDATA_LANGS.
_DEFAULT_LANGS = "zh,es,ru,ar,hi,pt,fr,de,ja,ko,id,tr"


def _aid(url: str) -> str:
    return "nd-" + hashlib.sha1(url.encode()).hexdigest()[:18]


def _topics() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    env = os.environ.get("MAAT_TOPICS")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    f = ROOT / "config" / "topics.txt"
    if f.exists():
        return [ln.strip() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return ["news"]


def _query(topics: list[str]) -> str:
    # NewsData ORs space-separated terms; quote multi-word phrases. One query covers all topics.
    return " OR ".join(f'"{t}"' for t in topics) if topics else "news"


async def main() -> None:
    load_dotenv(ROOT / ".env")
    if not newsdata.available():
        print("[newsdata] MAAT_NEWSDATA_KEY not set — skipping (the channel is dormant)")
        return
    maxrecords = int(os.environ.get("MAAT_NEWSDATA_MAXRECORDS", "10"))
    langs = [c.strip() for c in os.environ.get("MAAT_NEWSDATA_LANGS", _DEFAULT_LANGS).split(",") if c.strip()]
    query = _query(_topics())

    pool = await get_pool()
    seen = {r["url"] for r in await pool.fetch("select url from articles where url is not null")}
    gate_prompt = await prompts.active_text(pool, "source_gate", prompts.seed_default("source_gate"))
    known_good = frozenset(
        (r["source"] or "").lower().removeprefix("www.")
        for r in await pool.fetch("select distinct source from articles where source is not null")
    )
    denied = denied_sources(
        (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"])
        for r in await pool.fetch(
            "select data from events where type = 'admin.source.flagged' order by id"
        )
    )
    await pool.close()

    nc = await connect()
    new = dropped = 0
    gate_cache: dict = {}
    by_lang: Counter[str] = Counter()

    async def ingest(a: newsdata.NewsDataArticle) -> None:
        nonlocal new, dropped
        if a.url in seen:
            return
        seen.add(a.url)
        if a.domain in denied:
            dropped += 1
            return
        verdict = await asyncio.to_thread(
            accept_source, a.domain, a.title, prompt=gate_prompt, known_good=known_good, cache=gate_cache
        )
        if not verdict.accept:
            dropped += 1
            return
        if not a.body:
            return
        await publish(
            nc, "article.ingested", _aid(a.url),
            {
                "title": a.title, "source": a.domain, "language": a.language,
                "body": a.body, "url": a.url, "image_url": a.image_url,
                "provider": "newsdata", "country": a.country,
            },
        )
        new += 1
        by_lang[a.language or "?"] += 1

    # A broad pass (whatever's latest across languages) + one floor pass per target language.
    passes: list[tuple[str, str | None]] = [("broad", None)] + [(lng, lng) for lng in langs]
    for label, lang in passes:
        try:
            arts = await asyncio.to_thread(
                newsdata.search, query, language=lang, max_results=maxrecords
            )
        except Exception as e:  # noqa: BLE001 — a paid provider must not abort the run
            print(f"[newsdata] {label} unavailable: {e}", flush=True)
            continue
        before = new
        for a in arts:
            await ingest(a)
        print(f"[newsdata] {label} (+{new - before})", flush=True)

    await nc.flush()
    await nc.close()
    print(
        f"[newsdata] done: {new} new, {dropped} dropped across {len(passes)} passes. "
        f"langs={dict(by_lang.most_common())}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
