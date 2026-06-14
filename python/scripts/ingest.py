"""Publish one `article.ingested` event onto the bus (manual ingest / smoke).

Run: uv run python scripts/ingest.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from dotenv import load_dotenv

from maat.bus import connect
from maat.events import publish

ARTICLE_ID = "dn-iran-2026-06-02"
ARTICLE = {
    "title": "Iran Suspends Talks with U.S. Amid Israel's Attacks on Lebanon",
    "source": "Democracy Now",
    "language": "en",
    "body": (
        "Iranian diplomats have suspended talks with the United States after warning that "
        "Israel's attacks on Lebanon and the Gaza Strip could doom ongoing ceasefire "
        "negotiations with the Trump administration. Iranian Foreign Minister Abbas Araghchi "
        "said Monday that the U.S. had already violated its ceasefire with Iran when it imposed "
        "a naval siege on Iranian ports. He also said Israel's attacks on Lebanon constituted a "
        "ceasefire violation on a separate front."
    ),
}


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    nc = await connect()
    await publish(nc, "article.ingested", ARTICLE_ID, ARTICLE)
    await nc.flush()
    await nc.close()
    print(f"published article.ingested ({ARTICLE_ID})")


if __name__ == "__main__":
    asyncio.run(main())
