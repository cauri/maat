"""Curation agent (P5, issue #47): the feed-event shell around the pure core.

The de-US-centering re-rank logic is PURE and lives in ``maat.pipeline.curation``
(``Story``, ``curate``, ``anglosphere_share``, ``region_distribution``,
``_stories_from_payload``, and the DRAFT ``_DRAFT_GEOTAG_PROMPT``). This module
is only the agent shell — agents sit ABOVE the pipeline, so the dependency
points downward, never the reverse (#291/#293).

It subscribes to ``maat.events.feed.requested`` (or can be run as a batch pass
over the stories projection): it reads the feed payload, calls ``curate()``, and
emits ``feed.curated`` with the re-ordered story ids.

LLM enrichment is NOT wired here.  If a future pass needs to infer geography
from story text when metadata is absent, add it as a separate enrichment agent
that emits ``story.geo_inferred`` events; the curation agent can then read that
projection.  The DRAFT geo-tag prompt for such a step lives (gated off,
read-only) in ``maat.pipeline.curation``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from maat.pipeline.curation import (
    _stories_from_payload,
    anglosphere_share,
    curate,
    region_distribution,
)


async def handle(nc: Any, event: dict[str, Any]) -> None:
    """Consume ``feed.requested``, emit ``feed.curated`` with a re-ordered id list."""
    from maat.events import publish  # local import to keep the pure module importable without bus

    data = event.get("data", {})
    raw_stories = data.get("stories") or []
    if not raw_stories:
        return

    stories = _stories_from_payload(raw_stories)
    ordered = curate(stories)
    ordered_ids = [s.id for s in ordered]

    await publish(
        nc,
        "feed.curated",
        data.get("request_id", "feed"),
        {
            "request_id": data.get("request_id", "feed"),
            "story_ids": ordered_ids,
            "anglosphere_share": anglosphere_share(ordered),
            "region_distribution": region_distribution(ordered),
        },
    )
    print(
        f"[curation] {len(ordered)} stories curated; "
        f"anglosphere share {anglosphere_share(ordered):.0%}",
        flush=True,
    )


async def _run() -> None:
    from maat.bus import run_agent  # local import

    await run_agent("curation", "maat.events.feed.requested", handle)


def main() -> None:
    from dotenv import load_dotenv  # local import

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
