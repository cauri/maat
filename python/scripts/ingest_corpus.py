"""Publish every article in the corpus fixtures as `article.ingested` events.

Run: uv run python scripts/ingest_corpus.py [path-to-corpus.json]
With no argument, ingests every corpus/*.json (re-running re-extracts, so use on a fresh store).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from maat.bus import connect
from maat.events import publish

ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    load_dotenv(ROOT / ".env")
    if len(sys.argv) > 1:
        paths = [Path(sys.argv[1])]
    else:
        paths = sorted((ROOT / "corpus").glob("*.json"))
    nc = await connect()
    total = 0
    for path in paths:
        data = json.loads(path.read_text())
        for a in data["articles"]:
            await publish(
                nc,
                "article.ingested",
                a["id"],
                {
                    "title": a["title"],
                    "source": a["source"],
                    "language": a.get("language", "en"),
                    "body": a["body"],
                },
            )
        total += len(data["articles"])
        print(f"published {len(data['articles'])} articles from {path.name}")
    await nc.flush()
    await nc.close()
    print(f"published {total} articles total")


if __name__ == "__main__":
    asyncio.run(main())
