"""Run corroboration over the current claims (reads Postgres, prints). Manual / DRAFT.

Run: uv run python scripts/corroborate_run.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

from maat.pipeline.corroborate import ClaimRow, corroborate


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    pool = await asyncpg.create_pool(
        os.environ.get("DATABASE_URL", "postgresql://maat:maat@localhost:5432/maat")
    )
    arts = await pool.fetch("select id, source, body from articles")
    src = {r["id"]: r["source"] for r in arts}
    bodies = {r["id"]: r["body"] for r in arts}
    rows = await pool.fetch("select id, text, article_id from claims")
    await pool.close()
    claims = [
        ClaimRow(id=str(r["id"]), text=r["text"], article_id=r["article_id"], source=src.get(r["article_id"], ""))
        for r in rows
    ]
    results = corroborate(claims, bodies)
    print(f"{len(results)} corroborated fact-cluster(s):\n")
    for r in results:
        primary = " (+ primary source)" if r.has_primary else ""
        print(f"FACT: {r.fact[:74]}")
        print(f"  asserted by {len(r.sources)} sources -> {r.independent_originators} INDEPENDENT originators{primary}")
        for grp in r.originators:
            names = sorted({src.get(a, a) for a in grp})
            tag = "WIRE/COLLAPSED" if len(grp) > 1 else "independent"
            print(f"    [{tag}] {', '.join(names)}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
