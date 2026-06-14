"""Publish every article in a corpus fixture as an `article.ingested` event.

Run: uv run python scripts/ingest_corpus.py [path-to-corpus.json]
Default: corpus/resignation-scandal.json
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
DEFAULT = ROOT / "corpus" / "resignation-scandal.json"


async def main() -> None:
    load_dotenv(ROOT / ".env")
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    data = json.loads(path.read_text())
    nc = await connect()
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
    await nc.flush()
    await nc.close()
    print(f"published {len(data['articles'])} articles from {path.name}")


if __name__ == "__main__":
    asyncio.run(main())
