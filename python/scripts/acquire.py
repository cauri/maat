"""Acquire real articles for a query, fetch bodies, publish article.ingested (#33).

Primary source is GDELT (broad, global, multilingual); if it is down / rate-limited and yields
nothing usable, fall back to Apify (apify/rag-web-browser, requires APIFY_API_KEY).

Run: uv run python scripts/acquire.py "<query>" [maxrecords]
e.g. uv run python scripts/acquire.py "central bank interest rate sourcelang:English" 12
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

from dotenv import load_dotenv

from maat.acquire import apify
from maat.acquire.fetch import fetch_body
from maat.acquire.gdelt import search
from maat.bus import connect
from maat.events import publish

ROOT = Path(__file__).resolve().parents[2]


def _aid(url: str) -> str:
    return "gd-" + hashlib.sha1(url.encode()).hexdigest()[:18]


async def main() -> None:
    load_dotenv(ROOT / ".env")
    if len(sys.argv) < 2:
        print('usage: acquire.py "<query>" [maxrecords]')
        return
    query = sys.argv[1]
    maxrec = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    try:
        arts = search(query, maxrecords=maxrec, timespan="7d")
    except Exception as e:  # GDELT down / still rate-limited after retries
        print(f"GDELT unavailable: {e}")
        arts = []
    print(f"GDELT: {len(arts)} articles for {query!r}")
    nc = await connect()
    n = 0
    for a in arts:
        body = fetch_body(a.url)
        if not body:
            print(f"  skip (no body) {a.domain}")
            continue
        await publish(
            nc,
            "article.ingested",
            _aid(a.url),
            {"title": a.title, "source": a.domain, "language": a.language, "body": body, "url": a.url},
        )
        n += 1
        print(f"  + [{a.country or '?'}/{a.language or '?'}] {a.domain}: {a.title[:52]}")

    # Fallback: GDELT (and trafilatura) yielded nothing usable — try Apify (search + body in one).
    if n == 0 and apify.available():
        print("GDELT yielded nothing — falling back to Apify rag-web-browser")
        for fa in apify.search_and_fetch(query, max_results=maxrec):
            await publish(
                nc,
                "article.ingested",
                _aid(fa.url),
                {"title": fa.title, "source": fa.domain, "language": fa.language, "body": fa.body, "url": fa.url},
            )
            n += 1
            print(f"  + [apify/{fa.language or '?'}] {fa.domain}: {fa.title[:52]}")

    await nc.flush()
    await nc.close()
    print(f"acquired {n} articles")


if __name__ == "__main__":
    asyncio.run(main())
