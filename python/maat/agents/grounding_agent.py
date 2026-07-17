"""Primary-source grounding agent (#228, P3) — does each cluster's primary source back its fact?

Gated by ``MAAT_GROUNDING_LLM=1`` (no-op otherwise). For each cluster with a primary source that we
haven't grounded yet, it judges the fact against that primary source's article body (SUPPORTED /
CONTRADICTED / NOT_ADDRESSED), recomputes the confidence with that verdict (the primary lift is
earned only on genuine support; a contradiction multiplies it down), and emits ``cluster.grounded``
— maat-kerneld updates the cluster row (grounding + confidence), and the harvester carries the
verdict into ``cluster_snapshots`` so a contradiction resolves the fact to REFUTED over time.

Heuristic-first like ``agents.geotag_agent``: the model is consulted only for primary-bearing
clusters we haven't judged before, so spend scales with the genuinely-checkable tail. Runs after
corroborate and before harvest in the clock loop.

Run: uv run python -m maat.agents.grounding_agent

**Bounded per tick (#419).** Being incremental is not the same as being bounded. `done` stops us
re-judging a cluster, but says nothing about how many are judged in ONE tick — and each is an LLM
call, measured at 3.1s on the box, against a 1200s step timeout (~393 clusters). A restarted engine
faces a cold backlog well past that (the corroborate run alone yields 10,577 clusters), so the step
would be killed mid-work and reported TIMEOUT on every tick until it drained: a false alarm
indistinguishable from the outage the watchdog exists to catch. Each tick now grounds at most
``MAAT_GROUNDING_MAX_CLUSTERS`` and exits cleanly, reporting the backlog it left behind.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from maat.db import get_pool
from maat import prompts
from maat.bus import connect
from maat.events import CLUSTER_GROUNDED, publish
from maat.pipeline.corroborate import confidence_read, effective_originators, is_primary_source
from maat.pipeline.grounding import judge_grounding

ROOT = Path(__file__).resolve().parents[3]

# Clusters grounded per tick. judge_grounding is one LLM call, measured on the box at 3.1s, so the
# 1200s step fits ~393; 300 leaves headroom for the corpus load. Grounded clusters persist (the
# CLUSTER_GROUNDED events form `done`), so the backlog drains monotonically across ticks instead of
# the step being killed mid-work and reported TIMEOUT every time — see the module docstring.
_MAX_CLUSTERS = int(os.environ.get("MAAT_GROUNDING_MAX_CLUSTERS", "300"))

# Tiered authority for the ENGINE (#434) — gated OFF by default, same posture as the engine itself
# ("leave it built but off"). When "1": clusters WITHOUT a primary get an authority-seeking pass —
# find the fact's source of truth (paper / filing / court record / official release), NLI-gate it,
# and publish the grounding verdict. Each is a web-search LLM call (~10-20s), so its own small
# per-tick budget rides INSIDE the main one.
_AUTHORITY = os.environ.get("MAAT_GROUNDING_AUTHORITY", "0") == "1"
_MAX_AUTHORITY = int(os.environ.get("MAAT_GROUNDING_AUTHORITY_MAX", "15"))
# 15, not more: the two legs SHARE the 1200s step. Worst case 300×3.1s (llm leg) + 15×~15s
# (authority web searches) ≈ 1155s — inside the timeout with headroom. Raise only with the step's.


def _authority_verdict(fact: str, prompt_seed: str) -> tuple[str, str, str] | None:
    """Seek ONE cluster fact's source of truth (#434) → (verdict, evidence, domain) or None.

    The same standards as the Analyse leg, engine-side: the model proposes tier-1/2 authority
    citations (paper / filing / court record / official release); an NLI model judges each quote
    against the fact; an entailing quote is best-effort re-fetched and dropped if its content is
    wholly absent from the page; a CONTRADICTING quote (same grounding standard) yields
    "contradicted" — which the harvester folds toward REFUTED. Blocking (web search + NLI + fetch):
    callers run it BEFORE holding a NATS connection."""
    from maat.acquire.fetch import fetch_page
    from maat.pipeline import nli as nli_mod
    from maat.pipeline.analyse import judge_entailment_scored, quote_grounded
    from maat.pipeline.authority import parse_authority_citations
    from maat.providers.seam import claude_web_search

    if not nli_mod.available():
        return None  # never trust the search model's own mapping without the NLI gate
    prompt = prompt_seed.replace("{own_domain}", "unknown").replace("{claims}", f"1. {fact}")
    tool = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
    try:
        blocks = claude_web_search(prompt, tools=[tool], model="claude-sonnet-4-6")
    except Exception:  # noqa: BLE001 - one failed search skips the cluster, never the tick
        return None
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    cits = parse_authority_citations(text, 1)[0]
    for cit in sorted(cits, key=lambda c: c.tier):  # the document itself before the statement
        verdict, _strength = judge_entailment_scored(nli_mod.classify_pair, cit.quote, fact)
        if verdict not in ("entails", "contradicts"):
            continue
        try:
            page = fetch_page(cit.url, fast=True)
            body = page.body if page and page.body else None
        except Exception:  # noqa: BLE001 - unfetchable → judged on NLI alone, same as analyse
            body = None
        if body is not None and not quote_grounded(cit.quote, body):
            continue  # fetched, but the quote's content isn't on the page → mis-attributed
        return ("supported" if verdict == "entails" else "contradicted", cit.quote, cit.domain)
    return None


def _jload(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def _primary_article(article_ids: list[str], arts: dict[str, dict]) -> tuple[str, str] | None:
    """Pick the primary-source article among a cluster's articles → (source_name, body).

    Prefers the longest body when several primary articles are present (more to ground against).
    """
    best: tuple[str, str] | None = None
    for aid in article_ids:
        a = arts.get(aid)
        if a and is_primary_source(a.get("source") or ""):
            body = a.get("body") or ""
            if best is None or len(body) > len(best[1]):
                best = (a.get("source") or "", body)
    return best


async def main() -> None:
    if os.environ.get("MAAT_GROUNDING_LLM") != "1":
        print("grounding: MAAT_GROUNDING_LLM != 1 — disabled, nothing to do")
        return
    load_dotenv(ROOT / ".env")
    tenant = os.environ.get("MAAT_TENANT_ID", "cauri")
    pool = await get_pool()
    crows = await pool.fetch(
        "select id, fact, claim_ids, originators, has_primary, extremity, confidence "
        "from clusters where tenant_id = $1 and has_primary",
        tenant,
    )
    # #434 — the authority leg's candidates: clusters with NO primary among their articles. Bounded
    # in SQL; `done` still filters (a cluster grounded by either leg is never re-paid for).
    arows = []
    if _AUTHORITY:
        arows = await pool.fetch(
            "select id, fact, claim_ids, originators, has_primary, extremity, confidence "
            "from clusters where tenant_id = $1 and not has_primary limit $2",
            tenant, _MAX_AUTHORITY * 4,
        )
    claims = await pool.fetch("select id, article_id from claims")
    arts_rows = await pool.fetch("select id, source, body from articles")
    grounding_prompt = await prompts.active_text(pool, "grounding", prompts.seed_default("grounding"))
    authority_prompt = (
        await prompts.active_text(pool, "authority_search", prompts.seed_default("authority_search"))
        if _AUTHORITY else ""
    )
    # Clusters already grounded — don't pay to re-judge them (a changed claim set yields a new
    # cluster id, so a grown fact IS re-grounded; a stable one is judged once).
    done = {
        r["cid"]
        for r in await pool.fetch(
            "select distinct stream_id cid from events where type = $1", CLUSTER_GROUNDED
        )
    }
    await pool.close()

    art_of_claim = {str(r["id"]): str(r["article_id"]) for r in claims}
    arts = {str(r["id"]): {"source": r["source"] or "", "body": r["body"] or ""} for r in arts_rows}
    bodies = {aid: a["body"] for aid, a in arts.items()}
    srcs = {aid: a["source"] for aid, a in arts.items()}

    # #434 authority leg — ALL its blocking work (web search + NLI + fetch) happens BEFORE the
    # NATS connection is opened: a blocking call inside a publish loop starves the keepalive, the
    # server drops the link, and flush() discards everything (the gotcha that cost three agents
    # their entire output). Resolve first, hold the bus only for the fast publishes.
    auth_resolved: list[tuple[dict, str, str, str]] = []
    if _AUTHORITY:
        for r in [a for a in arows if a["id"] not in done][:_MAX_AUTHORITY]:
            got = await asyncio.to_thread(_authority_verdict, r["fact"] or "", authority_prompt)
            if got is not None:
                auth_resolved.append((r, *got))

    nc = await connect()
    judged = 0
    pending = [r for r in crows if r["id"] not in done]
    for r in pending:
        if judged >= _MAX_CLUSTERS:
            break  # tick budget spent — the rest is judged next tick (see the module docstring)
        claim_ids = [str(x) for x in _jload(r["claim_ids"])]
        article_ids = list(dict.fromkeys(art_of_claim[c] for c in claim_ids if c in art_of_claim))
        primary = _primary_article(article_ids, arts)
        if primary is None:
            continue  # flagged has_primary but no resolvable primary article body — leave ungrounded
        source_name, primary_body = primary
        verdict, evidence = judge_grounding(
            r["fact"] or "", source_name, primary_body, prompt=grounding_prompt
        )
        if not verdict:
            continue  # uncertain / error — leave the cluster ungrounded (confidence unchanged)
        # Recompute confidence with the verdict, consistently with corroborate: weight the
        # originator groups by sourcing quality, then read with the grounding signal.
        originators = [[str(a) for a in g] for g in _jload(r["originators"])]
        eff = effective_originators(originators, bodies, srcs)
        conf = confidence_read(eff, bool(r["has_primary"]), r["extremity"] or "notable", grounding=verdict)
        await publish(
            nc,
            CLUSTER_GROUNDED,
            r["id"],
            {
                "cluster_id": r["id"],
                "grounding": verdict,
                "confidence": conf,
                "evidence": evidence,
                "source": source_name,
                "method": "llm",
            },
            tenant,
        )
        judged += 1
    agrounded = 0
    for r, verdict, evidence, domain in auth_resolved:
        originators = [[str(a) for a in g] for g in _jload(r["originators"])]
        eff = effective_originators(originators, bodies, srcs)
        # primary=True: the authority WAS found (that is what this leg establishes) — the lift is
        # earned on support and multiplied down on contradiction, exactly like the llm leg.
        conf = confidence_read(eff, True, r["extremity"] or "notable", grounding=verdict)
        await publish(
            nc,
            CLUSTER_GROUNDED,
            r["id"],
            {
                "cluster_id": r["id"],
                "grounding": verdict,
                "confidence": conf,
                "evidence": evidence,
                "source": domain,
                "method": "authority_search",
            },
            tenant,
        )
        agrounded += 1
    await nc.flush()
    await nc.close()
    left = max(0, len(pending) - judged)
    # Always state the backlog: a bounded run that reports only what it DID is indistinguishable
    # from one that finished the work.
    print(
        f"grounding: judged {judged:,}/{len(pending):,} primary-bearing cluster(s)"
        + (f"; {left:,} left for the next tick" if left else "; backlog clear")
        + (f"; authority leg grounded {agrounded}/{len(auth_resolved)} resolved" if _AUTHORITY else "")
    )


if __name__ == "__main__":
    asyncio.run(main())
