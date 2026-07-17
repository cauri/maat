"""Hindsight reputation backfill (#435) — resolve ONE outlet's past claims against today's knowledge.

Run: uv run python scripts/hindsight_backfill.py <domain> [--articles 20] [--months 18] [--dry-run]

The exogenous anchor for reputation (see maat/learning/hindsight.py): sample the outlet's articles
across 1–2 YEARS of GDELT history (date-targeted windows — spread matters: 10 outcomes from one
week are not 10 independent samples), extract their claims with the SAME extractor the pipeline
uses, triage to the falsifiable + discriminating subset, resolve each against today's knowledge
(web search + the NLI agreement gate), and publish one ``fact.hindsight`` event per resolution.

DELIBERATELY ISOLATED from the canonical store (same posture as the Analyse surface): articles and
claims live in memory only — nothing lands in ``articles``/``claims``, live intake stays exactly as
paused, and the parked engine's corpus does not grow. The ONLY durable output is the
``fact.hindsight`` event stream, keyed stably per (source, fact) so re-runs re-assert rather than
double-count.

Publishing follows the NATS gotcha rule: ALL blocking work (GDELT, fetches, LLM, NLI) happens
before the connection opens; the bus is held only for the fast publishes.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from maat.acquire.content import fetch_article  # noqa: E402
from maat.acquire.gdelt import search_window  # noqa: E402
from maat.bus import connect  # noqa: E402
from maat.events import FACT_HINDSIGHT, publish  # noqa: E402
from maat.learning.hindsight import (  # noqa: E402
    hindsight_payload,
    hindsight_stream_id,
    resolve_claim,
    triage_claims,
)
from maat.pipeline.classify import classify_claims  # noqa: E402
from maat.pipeline.extract import extract_claims  # noqa: E402

# Claims resolved per run — the spend ceiling. ~15 resolutions ≈ the 10-outcome floor at the
# measured ~65% resolution rate (unmeasured — the pilot calibrates it; see #435).
_MAX_RESOLUTIONS = 20


def _windows(months: int, n: int) -> list[tuple[datetime, datetime]]:
    """``n`` windows spread EVENLY across the last ``months`` months (oldest first). Even spread is
    the decorrelation requirement: outcomes must sample different weeks/desks/topics."""
    now = datetime.now(timezone.utc)
    span = timedelta(days=months * 30 / max(1, n))
    return [(now - span * (i + 1), now - span * i) for i in reversed(range(n))]


def _gather(domain: str, articles_target: int, months: int) -> list[tuple[str, str, str]]:
    """(url, body, seendate) for up to ``articles_target`` articles spread across the window."""
    out: list[tuple[str, str, str]] = []
    per_window = max(1, articles_target // 10)
    for start, end in _windows(months, 10):
        if len(out) >= articles_target:
            break
        try:
            hits = search_window(f"domain:{domain}", start=start, end=end, maxrecords=8)
        except Exception as e:  # noqa: BLE001 - one dead window narrows the spread, never aborts
            print(f"  window {start:%Y-%m}: GDELT failed ({type(e).__name__})")
            continue
        taken = 0
        for h in hits:
            if taken >= per_window or len(out) >= articles_target:
                break
            try:
                body, _title = fetch_article(h.url)
            except Exception:  # noqa: BLE001
                body = None
            if body:
                out.append((h.url, body, h.seendate))
                taken += 1
        print(f"  window {start:%Y-%m}: {taken} article(s)")
    return out


def _claims_of(domain: str, gathered: list[tuple[str, str, str]]) -> list[dict]:
    """Own-voice factual claims from the gathered bodies (memory only) + their article context."""
    rows: list[dict] = []
    for url, body, seendate in gathered:
        try:
            claims = classify_claims(extract_claims(body, source_metadata=domain))
        except Exception as e:  # noqa: BLE001 - one bad article never sinks the run
            print(f"  extract failed for {url[:60]}: {type(e).__name__}")
            continue
        for c in claims:
            # Own-voice facts only: an outlet's reputation is what IT asserted, not what it quoted.
            if getattr(c, "kind", "fact") == "fact" and c.voice == "own":
                rows.append({"text": c.text, "url": url, "published": seendate})
    return rows


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("domain")
    ap.add_argument("--articles", type=int, default=20)
    ap.add_argument("--months", type=int, default=18)
    ap.add_argument("--max-resolutions", type=int, default=_MAX_RESOLUTIONS)
    ap.add_argument("--dry-run", action="store_true", help="resolve but do not publish")
    args = ap.parse_args()
    load_dotenv(ROOT.parent / ".env")

    from maat.pipeline import nli as nli_mod

    # Honour /prompts overrides (both DRAFT prompts are operator-editable); seeds as fallback.
    from maat import prompts as prompts_mod
    from maat.db import get_pool

    try:
        pool = await get_pool()
        triage_prompt = await prompts_mod.active_text(
            pool, "hindsight_triage", prompts_mod.seed_default("hindsight_triage"))
        resolve_prompt = await prompts_mod.active_text(
            pool, "hindsight_resolve", prompts_mod.seed_default("hindsight_resolve"))
        await pool.close()
    except Exception:  # noqa: BLE001 - no DB (dev box) → the code seeds
        triage_prompt = prompts_mod.seed_default("hindsight_triage")
        resolve_prompt = prompts_mod.seed_default("hindsight_resolve")

    nli = nli_mod.classify_pair if nli_mod.available() else None
    if nli is None:
        sys.exit("hindsight: NLI model unavailable — outcomes would be un-gated LLM say-so; abort")

    print(f"hindsight backfill: {args.domain} ({args.articles} articles / {args.months} months)")
    gathered = _gather(args.domain, args.articles, args.months)
    print(f"gathered {len(gathered)} article(s)")
    candidates = _claims_of(args.domain, gathered)
    print(f"own-voice factual claims: {len(candidates)}")

    kept: list[dict] = []
    for s in range(0, len(candidates), 10):  # triage in batches of 10
        batch = candidates[s : s + 10]
        for row, keep in zip(batch, triage_claims([r["text"] for r in batch],
                                             prompt=triage_prompt)):
            if keep:
                kept.append(row)
    print(f"triage kept {len(kept)} falsifiable+discriminating claim(s)")
    kept = kept[: args.max_resolutions]

    resolved: list[tuple[dict, dict]] = []
    tally = {"confirmed": 0, "refuted": 0, "unresolved": 0}
    for row in kept:
        res = resolve_claim(
            row["text"], source=args.domain, published=row["published"], nli=nli,
            prompt=resolve_prompt,
        )
        tally[res["outcome"]] += 1
        print(f"  [{res['outcome']:10}] {row['text'][:80]}")
        if res["outcome"] != "unresolved":
            resolved.append((row, res))
    print(f"resolved: {tally} → {len(resolved)} scoring outcome(s)")
    if args.max_resolutions and kept:
        rate = (tally["confirmed"] + tally["refuted"]) / len(kept)
        print(f"resolution rate this run: {rate:.0%} (the pilot number that sizes the fleet)")

    if args.dry_run:
        print("dry-run: nothing published")
        return
    if not resolved:
        print("no scoring outcomes — nothing to publish")
        return

    # Publish LAST, holding the bus only for the fast sends (the NATS keepalive gotcha).
    nc = await connect()
    for row, res in resolved:
        payload = hindsight_payload(
            source=args.domain, fact=row["text"], outcome=res["outcome"],
            evidence=res["evidence"], article_url=row["url"], published_at=row["published"],
        )
        await publish(
            nc, FACT_HINDSIGHT, hindsight_stream_id(args.domain, row["text"]), payload, "cauri"
        )
    await nc.flush()
    await nc.close()
    print(f"published {len(resolved)} fact.hindsight event(s) for {args.domain}")


if __name__ == "__main__":
    asyncio.run(main())
