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

from maat.acquire import apify
from maat.acquire.fetch import fetch_body
from maat.acquire.gdelt import search
from maat.bus import connect
from maat.clocks import is_paused
from maat.events import publish

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
    await pool.close()

    nc = await connect()
    new = 0
    for topic in topics:
        try:
            arts = search(topic, maxrecords=15, timespan="1d")
        except Exception as e:  # GDELT down / rate-limited
            print(f"[{topic}] GDELT unavailable: {e}")
            arts = []
        got = 0
        for a in arts:  # GDELT gives metadata; fetch bodies for unseen URLs
            if a.url in seen:
                continue
            body = fetch_body(a.url)
            if not body:
                continue
            await publish(nc, "article.ingested", _aid(a.url),
                          {"title": a.title, "source": a.domain, "language": a.language, "body": body, "url": a.url})
            seen.add(a.url)
            new += 1
            got += 1
        # Always run a small Apify pass: its web search surfaces primary/authoritative sources —
        # the issuer's own release (e.g. ecb.europa.eu) — that the news-only GDELT stream misses
        # (#108). When GDELT came back empty it widens to a full fallback. MAAT_PRIMARY_PASS=0 opts
        # out (Apify costs credits per call).
        if apify.available() and os.environ.get("MAAT_PRIMARY_PASS", "1") != "0":
            for fa in apify.search_and_fetch(topic, max_results=10 if got == 0 else 5):
                if fa.url in seen:
                    continue
                await publish(nc, "article.ingested", _aid(fa.url),
                              {"title": fa.title, "source": fa.domain, "language": fa.language, "body": fa.body, "url": fa.url})
                seen.add(fa.url)
                new += 1
                got += 1
        print(f"[{topic}] +{got} new")
    await nc.flush()
    await nc.close()
    print(f"tick: {new} new articles across {len(topics)} topics")


if __name__ == "__main__":
    asyncio.run(main())
