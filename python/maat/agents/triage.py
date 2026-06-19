"""Feedback triage agent (P7, issue #58): the event shell around the pure core.

The classification core is PURE and lives in ``maat.pipeline.triage``
(``classify``, ``triage``, ``TriageResult``, ``CATEGORIES``, ``ROUTES``, the
rule table, and the DRAFT ``TRIAGE_LLM_PROMPT``). This module is only the agent
shell — agents sit ABOVE the pipeline, so the dependency points downward, never
the reverse (#291/#293).

It classifies each ``feedback.submitted`` event and routes it either to the
REVIEW QUEUE (for operator action) or flags it as AUTO-FIXABLE (a PR can be
generated without human gatekeeping), and serves the two routed queues read back
from the event log.

Run standalone (batch over the unprocessed queue)::

    uv run python -m maat.agents.triage
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from maat.pipeline.triage import triage
from maat.serving.feedback import FEEDBACK_SUBMITTED, FEEDBACK_TRIAGED, record_triage, routed_queue


# ---------------------------------------------------------------------------
# Review-queue view (read from events — no projection table)
# ---------------------------------------------------------------------------


async def review_queue(pool: Any, *, limit: int = 200, tenant_id: str = "cauri") -> list[dict]:
    """Return all feedback items currently in the review queue.

    Reads the latest ``feedback.triaged`` event per item and filters to route='review',
    then joins back to the original ``feedback.submitted`` event for context.
    This is a read-only projection: the event log is the source of truth.
    """
    return await routed_queue(pool, route="review", limit=limit, tenant_id=tenant_id)


async def auto_fix_queue(pool: Any, *, limit: int = 200, tenant_id: str = "cauri") -> list[dict]:
    """Return all feedback items flagged as auto-fixable."""
    return await routed_queue(pool, route="auto-fix", limit=limit, tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Batch-triage pass (standalone run)
# ---------------------------------------------------------------------------


async def _run_batch(pool: Any, nc: Any) -> None:
    """Process all un-triaged ``feedback.submitted`` events."""
    # Find items that have a submitted event but no triage event yet
    submitted = await pool.fetch(
        "select stream_id, data from events where type = $1 order by id asc",
        FEEDBACK_SUBMITTED,
    )
    triaged_ids = {
        r["item_id"]
        for r in await pool.fetch(
            "select data->>'item_id' item_id from events where type = $1",
            FEEDBACK_TRIAGED,
        )
    }

    pending = [
        r for r in submitted
        if r["stream_id"] not in triaged_ids
    ]

    for row in pending:
        d = json.loads(row["data"]) if isinstance(row["data"], str) else dict(row["data"])
        result = triage(row["stream_id"], d.get("text", ""), d.get("category_hint", ""))
        await record_triage(
            pool,
            nc,
            item_id=result.item_id,
            category=result.category,
            route=result.route,
            confidence=result.confidence,
            reason=result.reason,
            auto_fixable=result.auto_fixable,
        )
        print(
            f"[triage] {result.item_id}: {result.category} → {result.route} "
            f"(conf={result.confidence:.2f})",
            flush=True,
        )

    print(f"[triage] processed {len(pending)} pending item(s)", flush=True)


async def _main() -> None:
    from dotenv import load_dotenv

    from maat.bus import connect
    from maat.db import get_pool

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    pool = await get_pool()
    nc = await connect()
    await _run_batch(pool, nc)
    await nc.flush()
    await nc.close()
    await pool.close()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
