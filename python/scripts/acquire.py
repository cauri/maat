"""Acquire real articles for a query, fetch bodies, publish article.ingested (#33).

Primary source is GDELT (broad, global, multilingual); if it is down / rate-limited and yields
nothing usable, fall back to Apify (apify/rag-web-browser, requires APIFY_API_KEY).

Run: uv run python scripts/acquire.py "<query>" [maxrecords]
e.g. uv run python scripts/acquire.py "central bank interest rate sourcelang:English" 12
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

from maat import ids
from maat.acquire import apify
from maat.acquire.clean import clean_article
from maat.acquire.fetch import fetch_article
from maat.acquire.gdelt import search
from maat.bus import connect
from maat.events import publish

ROOT = Path(__file__).resolve().parents[2]


def _aid(url: str) -> str:
    return ids.article_id(url, "gd")


async def main() -> None:
    load_dotenv(ROOT / ".env")
    if len(sys.argv) < 2:
        print('usage: acquire.py "<query>" [maxrecords]')
        return
    query = sys.argv[1]
    maxrec = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    try:
        # Off the event loop: blocking httpx keeps the NATS client's flush/ping alive (see clock.py).
        arts = await asyncio.to_thread(search, query, maxrecords=maxrec, timespan="7d")
    except Exception as e:  # GDELT down / still rate-limited after retries
        print(f"GDELT unavailable: {e}")
        arts = []
    print(f"GDELT: {len(arts)} articles for {query!r}")
    nc = await connect()
    n = 0
    for a in arts:
        body, image_url = await asyncio.to_thread(fetch_article, a.url)
        if not body:
            print(f"  skip (no body) {a.domain}")
            continue
        ct, cb = clean_article(a.title, body, a.domain)  # strip scraped boilerplate (#33)
        await publish(
            nc,
            "article.ingested",
            _aid(a.url),
            {"title": ct, "source": a.domain, "language": a.language, "body": cb,
             "url": a.url, "image_url": image_url},
        )
        n += 1
        print(f"  + [{a.country or '?'}/{a.language or '?'}] {a.domain}: {a.title[:52]}")

    # Fallback: GDELT (and trafilatura) yielded nothing usable — try Apify (search + body in one).
    if n == 0 and apify.available():
        print("GDELT yielded nothing — falling back to Apify rag-web-browser")
        for fa in await asyncio.to_thread(apify.search_and_fetch, query, max_results=maxrec):
            ct, cb = clean_article(fa.title, fa.body, fa.domain)  # strip scraped boilerplate (#33)
            await publish(
                nc,
                "article.ingested",
                _aid(fa.url),
                {"title": ct, "source": fa.domain, "language": fa.language, "body": cb,
                 "url": fa.url, "image_url": fa.image},
            )
            n += 1
            print(f"  + [apify/{fa.language or '?'}] {fa.domain}: {fa.title[:52]}")

    await nc.flush()
    await nc.close()
    print(f"acquired {n} articles")


if __name__ == "__main__":
    asyncio.run(main())
