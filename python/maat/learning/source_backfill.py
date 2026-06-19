"""Per-source backfill — the #241 "backfilling" lifecycle stage, shared by the manual CLI
(scripts/backfill_source.py) and the registry agent's auto-trigger.

Given a source, it pulls the outlet's recent published history (maat.acquire.history), gates each
candidate to a credible publisher, and ingests the survivors as ordinary ``article.ingested``
events tagged ``provider: backfill`` + ``backfill_run_id`` + the outlet. From there the NORMAL
pipeline takes over — the agents extract + classify them, corroborate clusters them, and the
registry folds reputation and flips the source ``active``. No special path: the backfill is just
an acquisition source that happens to be scoped to one outlet's past.

The run emits ``source.state_changed(state=backfilling, run_id, cost_usd)`` so the lifecycle + the
/sources cost are observable. ``backfill_run_id`` lets us attribute the downstream LLM cost back to
the run (maat.serving.spend.backfill_run_cost)."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field

from maat.acquire.history import fetch_source_history
from maat.acquire.source_gate import accept_source
from maat.events import SOURCE_STATE_CHANGED, publish
from maat import ids

# Apify is the only history channel that costs per result (GDELT is free; NewsData is plan-flat).
# Rough per-result event price for the rag-web-browser actor — the actual figure is on /spend.
_APIFY_PER_RESULT_USD = 0.005


def _aid(url: str) -> str:
    return ids.article_id(url, "bf")


@dataclass
class BackfillResult:
    source: str
    run_id: str
    fetched: int = 0
    ingested: int = 0
    gated_out: int = 0
    duplicate: int = 0
    no_body: int = 0
    by_channel: dict[str, int] = field(default_factory=dict)
    acquisition_usd: float = 0.0


def run_id_for(source: str, stamp: str) -> str:
    """Stable run id from the source + a caller-supplied timestamp (callers pass the time so this
    stays pure / testable)."""
    return ids.backfill_run_id(source, stamp)


async def run_backfill(
    nc,
    source: str,
    *,
    run_id: str,
    at: str,
    depth: int,
    gate_prompt: str,
    known_good: frozenset = frozenset(),
    denied: set | None = None,
    seen: set | None = None,
) -> BackfillResult:
    """Fetch the source's history → gate → ingest (tagged). Emits backfilling state up front and a
    final state_changed carrying the acquisition cost. Returns per-step counts for the log."""
    denied = denied or set()
    seen = seen if seen is not None else set()
    res = BackfillResult(source=source, run_id=run_id)
    await publish(nc, SOURCE_STATE_CHANGED, source,
                  {"source": source, "state": "backfilling", "run_id": run_id, "at": at})

    arts = await fetch_source_history(source, depth=depth)
    res.fetched = len(arts)
    gate_cache: dict = {}
    channels: Counter[str] = Counter()
    for a in arts:
        if a.url in seen:
            res.duplicate += 1
            continue
        seen.add(a.url)
        if a.domain in denied:
            res.gated_out += 1
            continue
        verdict = await asyncio.to_thread(
            accept_source, a.domain, a.title, prompt=gate_prompt, known_good=known_good, cache=gate_cache
        )
        if not verdict.accept:
            res.gated_out += 1
            continue
        if not a.body:
            res.no_body += 1
            continue
        await publish(
            nc, "article.ingested", _aid(a.url),
            {
                "title": a.title, "source": a.domain, "language": a.language,
                "body": a.body, "url": a.url, "image_url": a.image_url,
                "provider": "backfill", "backfill": "true", "backfill_run_id": run_id,
                "channel": a.channel,
            },
        )
        res.ingested += 1
        channels[a.channel] += 1
    res.by_channel = dict(channels)
    res.acquisition_usd = round(channels.get("apify", 0) * _APIFY_PER_RESULT_USD, 4)
    return res
