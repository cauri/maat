"""RSS acquisition driver (#238) — pull the balanced multipolar feed set into the live pipeline.

Each feed is a hand-vetted news outlet, so we skip the LLM source-gate (inclusion in the list
IS the vetting) and only honour the operator deny-list (#187). Items flow through the normal
extract -> classify -> corroborate -> reputation path tagged ``provider: rss`` plus the feed's
``alignment`` (independent | public | state) and country, so the independence layer can weight
state-aligned outlets and never count them as independent corroboration (#41).

Run:  uv run python scripts/acquire_rss.py        (or `make acquire-rss`)
Env:  MAAT_RSS_PER_FEED (default 12)  — max items pulled per feed per run.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

from maat import ids
from maat.db import get_pool
from maat.acquire.clean import clean_article
from maat.acquire.fetch import fetch_article
from maat.acquire.rss import fetch_feed, load_feeds
from maat.bus import connect
from maat.events import publish
from maat.serving.source_flags import denied_sources

ROOT = Path(__file__).resolve().parents[2]


def _aid(url: str) -> str:
    return ids.article_id(url, "rss")


async def main() -> None:
    load_dotenv(ROOT / ".env")
    per_feed = int(os.environ.get("MAAT_RSS_PER_FEED", "12"))

    pool = await get_pool()
    seen = {r["url"] for r in await pool.fetch("select url from articles where url is not null")}
    denied = denied_sources(
        (json.loads(r["data"]) if isinstance(r["data"], str) else r["data"])
        for r in await pool.fetch(
            "select data from events where type = 'admin.source.flagged' order by id"
        )
    )
    await pool.close()

    feeds = load_feeds(ROOT / "config" / "feeds.txt")
    nc = await connect()
    new = dropped = 0
    by_lang: Counter[str] = Counter()
    by_country: Counter[str] = Counter()
    for feed in feeds:
        try:
            items = await asyncio.to_thread(fetch_feed, feed, limit=per_feed)
        except Exception as e:  # noqa: BLE001 - one dead/blocked feed must not abort the run
            print(f"[rss] {feed.label} unavailable: {e}", flush=True)
            continue
        got = 0
        for it in items:
            if it.url in seen:
                continue
            if it.source in denied:  # operator-denied source (#187)
                seen.add(it.url)
                dropped += 1
                continue
            body, image = await asyncio.to_thread(fetch_article, it.url)
            if not body:
                continue
            title, body = clean_article(it.title, body, it.source)  # strip scraped boilerplate (#33)
            await publish(
                nc, "article.ingested", _aid(it.url),
                {
                    "title": title, "source": it.source, "language": it.language,
                    "body": body, "url": it.url, "image_url": image,
                    "provider": "rss", "alignment": it.alignment, "country": it.country,
                },
            )
            seen.add(it.url)
            new += 1
            got += 1
            by_lang[it.language or "?"] += 1
            by_country[it.country or "?"] += 1
        print(f"[rss] {feed.label} (+{got})", flush=True)
    await nc.flush()
    await nc.close()
    print(
        f"[rss] done: {new} new, {dropped} denied across {len(feeds)} feeds. "
        f"langs={dict(by_lang.most_common())} countries={dict(by_country.most_common())}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
