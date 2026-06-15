"""Ingestion clock (#34) — one incremental tick: acquire NEW articles for the tracked topics.

Skips URLs already ingested (incremental deltas), so it's safe to run on a schedule (cron /
systemd timer). Deliberately a single pass — cadence, and therefore cost, are the operator's
choice, not a hardcoded daemon. The full pipeline (kernel + agents) must be running wherever
the tick runs, so the freshly-ingested articles get extracted, classified, and corroborated.

Topics: CLI args, else MAAT_TOPICS (comma-separated), else config/topics.txt (one per line).
Run: uv run python scripts/clock.py ["topic" ...]   (or `make tick`)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat import prompts
from maat.acquire import apify
from maat.acquire.fetch import fetch_article
from maat.acquire.gdelt import search
from maat.bus import connect
from maat.clocks import is_paused
from maat.events import publish
from maat.serving.topics import news_queries

ROOT = Path(__file__).resolve().parents[2]


def _aid(url: str) -> str:
    return "gd-" + hashlib.sha1(url.encode()).hexdigest()[:18]


def _topics() -> list[str]:
    if len(sys.argv) > 1:
        return sys.argv[1:]
    env = os.environ.get("MAAT_TOPICS")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    f = ROOT / "config" / "topics.txt"
    if f.exists():
        return [ln.strip() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    return []


async def main() -> None:
    load_dotenv(ROOT / ".env")
    topics = _topics()
    if not topics:
        print("no topics — pass args, set MAAT_TOPICS, or fill config/topics.txt")
        return
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    clk = await pool.fetch(
        "select data from events where type = 'admin.clock.set' order by id desc limit 20"
    )
    if is_paused([json.loads(r["data"]) if isinstance(r["data"], str) else r["data"] for r in clk]):
        await pool.close()
        print("ingestion clock paused (admin.clock.set) — skipping tick")
        return
    seen = {r["url"] for r in await pool.fetch("select url from articles where url is not null")}
    # Resolve the operator's active acquisition-query prompt (P8) before the pool closes.
    queries_prompt = await prompts.active_text(
        pool, "acquire_queries", prompts.seed_default("acquire_queries")
    )
    await pool.close()

    nc = await connect()
    new = 0
    use_apify = apify.available() and os.environ.get("MAAT_PRIMARY_PASS", "1") != "0"
    paced = 0  # global GDELT-call counter — GDELT throttles ~1 query/5s, so space ALL calls out
    for topic in topics:
        # Turn the interest into recent-NEWS queries (LLM) — searching the literal interest pulls
        # SEO/blog/listicle junk, not news. Blocking (one LLM call); run off the event loop.
        queries = await asyncio.to_thread(news_queries, topic, prompt=queries_prompt)
        print(f"[{topic}] → {queries}", flush=True)
        got = 0
        for q in queries:
            if paced:  # space successive GDELT calls to dodge its 429 back-off (a blocking sleep)
                await asyncio.sleep(5)
            paced += 1
            try:
                # search()/fetch_article() block (httpx + trafilatura). Run them OFF the event loop
                # — otherwise a multi-second fetch starves the NATS client's flush/ping tasks, the
                # connection drops, and published articles are silently lost (the 83→7 bug).
                arts = await asyncio.to_thread(search, q, maxrecords=15, timespan="1d")
            except Exception as e:  # GDELT down / rate-limited
                print(f"  [{q}] GDELT unavailable: {e}", flush=True)
                arts = []
            for a in arts:  # GDELT gives metadata; fetch body + lead image (#1) for unseen URLs
                if a.url in seen:
                    continue
                body, image_url = await asyncio.to_thread(fetch_article, a.url)
                if not body:
                    continue
                await publish(nc, "article.ingested", _aid(a.url),
                              {"title": a.title, "source": a.domain, "language": a.language,
                               "body": body, "url": a.url, "image_url": image_url})
                seen.add(a.url)
                new += 1
                got += 1
            # Apify pass per query: its web search surfaces primary/authoritative sources GDELT
            # misses (#108, e.g. the issuer's own release). MAAT_PRIMARY_PASS=0 opts out (credits).
            if use_apify:
                items = await asyncio.to_thread(apify.search_and_fetch, q, max_results=5)
                for fa in items:
                    if fa.url in seen:
                        continue
                    await publish(nc, "article.ingested", _aid(fa.url),
                                  {"title": fa.title, "source": fa.domain, "language": fa.language,
                                   "body": fa.body, "url": fa.url, "image_url": fa.image})
                    seen.add(fa.url)
                    new += 1
                    got += 1
        print(f"[{topic}] +{got} new", flush=True)
    await nc.flush()
    await nc.close()
    print(f"tick: {new} new articles across {len(topics)} topics", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
