"""Feedback triage agent (P7, issue #58).

Classifies each ``feedback.submitted`` event into one of the feedback categories and routes
it either to the REVIEW QUEUE (for operator action) or flags it as AUTO-FIXABLE (a PR can
be generated without human gatekeeping).

Architecture
------------
The classification core is PURE and deterministic — keyword/rule-based.  An optional LLM
refinement path is defined below as a DRAFT prompt (flagged for cauri review); it is disabled
by default and must be explicitly enabled.

Categories
~~~~~~~~~~
- ``veracity-dispute``  — user challenges the confidence score or corroboration of a claim
- ``source-quality``    — concern about a specific source's reliability/bias
- ``bug``               — something is broken in the UI or pipeline
- ``ui``                — layout / UX / display issue (not a correctness problem)
- ``topic-request``     — user wants a topic / region / language added

Routes
~~~~~~
- ``review``     — operator must act (veracity, source-quality, ambiguous items)
- ``auto-fix``   — safe to generate a PR without human sign-off (clear bugs, trivial UI fixes)

Run standalone (batch over the unprocessed queue)::

    uv run python -m maat.agents.triage
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from maat.serving.feedback import FEEDBACK_SUBMITTED, FEEDBACK_TRIAGED, record_triage, routed_queue

# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

CATEGORIES = (
    "veracity-dispute",
    "source-quality",
    "bug",
    "ui",
    "topic-request",
)

ROUTES = ("review", "auto-fix")


# DRAFT prompt — flag for cauri review.
# Defined as a real constant so it is surfaced READ-ONLY in the operator console, but it is
# intentionally NOT used by live code: the rule-based classifier below stays the default. Do not
# wire this into the pipeline without cauri review.
TRIAGE_LLM_PROMPT = """
You are a feedback triage assistant for Maat, a veracity-weighted news feed.
Given this user feedback:

  {text}

Classify it into exactly one of these categories:
  - veracity-dispute  (challenges a confidence score or claim accuracy)
  - source-quality    (concern about a specific outlet's reliability)
  - bug               (technical breakage in the UI or pipeline)
  - ui                (cosmetic / layout issue, not a correctness problem)
  - topic-request     (wants a new topic, region, or language added)

Return ONLY a JSON object: {{"category": "<category>", "reason": "<one sentence>"}}.
"""

_VALID_CATEGORIES = {"veracity-dispute", "source-quality", "bug", "ui", "topic-request"}
# Only clearly-mechanical categories can be auto-fixed without sign-off (#77 untrusted input).
_AUTO_FIXABLE_CATEGORIES = {"bug", "ui"}


def _llm_triage(text: str) -> tuple[str, str] | None:
    """DRAFT LLM triage refinement (#189) — gated by MAAT_TRIAGE_LLM. Classify the feedback via the
    bulk model; returns (category, reason) or None on any error (the rule pass stays the fallback).
    """
    if os.environ.get("MAAT_TRIAGE_LLM") != "1":
        return None
    try:
        from maat.providers.seam import claude_complete

        # Sonnet (cauri): routing user feedback to auto-fix vs review is a judgement call, so it
        # runs on Sonnet, not the cheap tier. Low volume (one call per feedback item). Rules below
        # stay the fallback on any error.
        reply = claude_complete(TRIAGE_LLM_PROMPT.format(text=text[:2000]), model="claude-sonnet-4-6")
        raw = reply.text
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        cat = str(data.get("category", "")).strip().lower()
        if cat in _VALID_CATEGORIES:
            return cat, (str(data.get("reason", "")).strip() or "LLM triage")
    except Exception:
        pass
    return None


@dataclass(frozen=True)
class TriageResult:
    item_id: str
    text: str
    category: str
    route: str
    confidence: float   # rule confidence 0.0-1.0
    reason: str         # human-readable trace for audit
    auto_fixable: bool


# ---------------------------------------------------------------------------
# Pure classification core (keyword / rule-based, fully deterministic)
# ---------------------------------------------------------------------------

# Each rule: (category, compiled-pattern, base-confidence, auto_fixable)
# All rules are evaluated; winner is determined by (match_count, confidence) descending.
# This lets a category that hits multiple keywords beat a single high-confidence hit,
# which produces more natural triage for mixed signals like "layout broken on mobile".
_RULES: list[tuple[str, re.Pattern[str], float, bool]] = [
    # --- veracity disputes ---
    (
        "veracity-dispute",
        re.compile(
            r"\b(wrong|incorrect|inaccurate|misleading|false|confidence|score|corrobor\w*|"
            r"veracity|well.corroborated|thinly.sourced|disputed|fact.check)\b",
            re.IGNORECASE,
        ),
        0.85,
        False,   # always needs human review
    ),
    # --- source quality ---
    (
        "source-quality",
        re.compile(
            r"\b(source|outlet|publisher|biased|unreliable|propaganda|credibility|"
            r"trust|wire|reuters|ap |afp|bbc|fox|cnn|tabloid)\b",
            re.IGNORECASE,
        ),
        0.80,
        False,   # source changes are operator decisions
    ),
    # --- bugs (technical breakage) ---
    (
        "bug",
        re.compile(
            r"\b(crash\w*|error|exception|doesn.t work|not working|"
            r"fails|500|404|timeout|blank page|spinne?r|freeze|stuck)\b",
            re.IGNORECASE,
        ),
        0.82,
        True,    # clear bug reports → PR candidate
    ),
    # --- UI / cosmetic ---
    (
        "ui",
        re.compile(
            r"\b(layout|display|render|font|colour|color|style|button|click|"
            r"mobile|responsive|overlap|overflow|icon|spacing|margin|padding|"
            r"dark.mode|light.mode|alignment|misaligned|broken\b)\b",
            re.IGNORECASE,
        ),
        0.78,
        True,    # cosmetic fix → auto-PR candidate
    ),
    # --- topic requests ---
    (
        "topic-request",
        re.compile(
            r"\b(add|include|cover|more|track|monitor|want|wish|"
            r"topic|region|country|language|category|section)\b",
            re.IGNORECASE,
        ),
        0.70,
        False,   # topic expansion → editorial / operator decision
    ),
]

# Fallback when no rule matches
_FALLBACK_CATEGORY = "veracity-dispute"
_FALLBACK_CONFIDENCE = 0.40


def _count_matches(pattern: re.Pattern[str], text: str) -> int:
    """Count how many distinct keyword hits exist in ``text`` for a rule."""
    return len(pattern.findall(text))


def classify(text: str, category_hint: str = "") -> TriageResult:
    """Pure classification function.  No I/O, no LLM.  Deterministic for any input.

    ``category_hint`` is a client-supplied label (e.g. from a dropdown) that bumps confidence
    when it agrees with the rule match, or breaks ties when no rule matches.

    Winner is chosen by (match_count, confidence) descending — a category that hits more
    keywords in the text beats one that hits fewer, even if the latter has a higher base score.
    """
    lowered = text.lower()
    hint = category_hint.strip().lower()

    best_cat: str = ""
    best_conf: float = 0.0
    best_reason: str = ""
    best_auto: bool = False
    best_hits: int = 0

    for cat, pattern, base_conf, auto in _RULES:
        hits = _count_matches(pattern, lowered)
        if not hits:
            continue
        conf = base_conf
        m_example = pattern.search(lowered)
        reason = f"matched {hits} keyword(s) incl. '{m_example.group() if m_example else '?'}'"
        if hint and hint == cat:
            conf = min(conf + 0.08, 0.97)
            reason += f"; client hint='{hint}' agrees"
        # prefer more hits; break ties with confidence
        if (hits, conf) > (best_hits, best_conf):
            best_cat = cat
            best_conf = conf
            best_reason = reason
            best_auto = auto
            best_hits = hits

    # No rule matched — honour the client hint or fall back
    if not best_cat:
        if hint in CATEGORIES:
            best_cat = hint
            best_conf = 0.55
            best_reason = f"no rule match; client hint='{hint}'"
            # check if the hinted category is auto_fixable
            best_auto = hint in ("bug", "ui")
        else:
            best_cat = _FALLBACK_CATEGORY
            best_conf = _FALLBACK_CONFIDENCE
            best_reason = "no rule match; defaulting to veracity-dispute for review"
            best_auto = False

    route = "auto-fix" if best_auto else "review"

    # Ambiguity guard: low-confidence auto-fix goes to review instead
    if route == "auto-fix" and best_conf < 0.65:
        route = "review"
        best_reason += "; low confidence → escalated to review"
        best_auto = False

    # DRAFT LLM refinement (#189) — gated by MAAT_TRIAGE_LLM. When enabled + available, the LLM's
    # category/reason override the rule pass (rules remain the fallback). Untrusted input (#77):
    # routing still flows through the auto-fix-only-if-mechanical + ambiguity guard below.
    llm = _llm_triage(text)
    if llm is not None:
        best_cat, best_reason = llm
        best_conf = max(best_conf, 0.8)
        best_auto = best_cat in _AUTO_FIXABLE_CATEGORIES
        route = "auto-fix" if best_auto else "review"
        if route == "auto-fix" and best_conf < 0.65:
            route = "review"
            best_auto = False

    return TriageResult(
        item_id="",   # caller fills this in
        text=text,
        category=best_cat,
        route=route,
        confidence=best_conf,
        reason=best_reason,
        auto_fixable=best_auto,
    )


def triage(item_id: str, text: str, category_hint: str = "") -> TriageResult:
    """Classify a feedback item and return a complete TriageResult with item_id set."""
    r = classify(text, category_hint)
    return TriageResult(
        item_id=item_id,
        text=r.text,
        category=r.category,
        route=r.route,
        confidence=r.confidence,
        reason=r.reason,
        auto_fixable=r.auto_fixable,
    )


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
