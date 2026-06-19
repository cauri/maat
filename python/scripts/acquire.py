"""Unified acquisition entrypoint (#290): ``acquire.py --source rss|newsdata|locales|gdelt``.

Each source's fetch + the fields it carries live in ``maat/acquire/<source>.py``; the shared
id → dedup → deny → [source-gate] → fetch → clean → ``publish(article.ingested)`` orchestration
lives in ``maat/acquire/ingest.py`` + ``drivers.py``. The per-source scripts (``acquire_rss.py`` …)
are thin ``--source`` aliases of this one, kept so the prod clock + ``make`` targets keep working.

Run:
  uv run python scripts/acquire.py --source rss          (or `make acquire-rss`)
  uv run python scripts/acquire.py --source newsdata
  uv run python scripts/acquire.py --source locales
  uv run python scripts/acquire.py --source gdelt --query "central bank interest rate" --max 12
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

from maat.acquire.drivers import SOURCES, acquire

ROOT = Path(__file__).resolve().parents[2]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Acquire articles from one source into the pipeline (#290).")
    p.add_argument("--source", required=True, choices=SOURCES, help="acquisition channel")
    p.add_argument("--query", default="", help="search query (gdelt source only)")
    p.add_argument("--max", type=int, default=None, dest="maxrecords",
                   help="records per query/feed (default: per-source env/constant)")
    p.add_argument("--topics", default=None,
                   help="comma-separated topics override (newsdata/locales; default MAAT_TOPICS/config)")
    return p.parse_args(argv)


async def _main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    load_dotenv(ROOT / ".env")
    topics = [t.strip() for t in args.topics.split(",") if t.strip()] if args.topics else None
    await acquire(
        args.source, root=ROOT,
        query=args.query, maxrecords=args.maxrecords, topics=topics,
    )


if __name__ == "__main__":
    asyncio.run(_main())
