"""Hindsight reputation (#435) — resolve an outlet's PAST claims against TODAY'S knowledge.

cauri's design: "History often throws light on what was false … look back at what they said and
what turned out to be true gives us a source of truth we can score on." This is where reputation's
exogenous anchor comes from: corroboration outcomes are the corpus agreeing with itself (and are
discounted in the fold accordingly — see ``reputation._CORROBORATION_WEIGHT``); a hindsight outcome
is a claim old enough for the world to have answered, checked against primary evidence.

The pipeline (driven by ``scripts/hindsight_backfill.py``, one-shot per outlet, fully ISOLATED from
the canonical store — articles live in memory only, nothing lands in ``articles``/``claims``):

  1. TRIAGE — own-voice factual claims only, then an LLM screen for the two properties that make a
     claim reputation-bearing: FALSIFIABLE (specific enough that evidence could prove it false) and
     DISCRIMINATING (mundane claims resolve true by construction and measure nothing). Measured on
     real extractions: ~32% of own-voice facts survive; the rest would be spend without signal.
  2. RESOLVE — a web-search pass seeking today's authoritative answer (the tier ladder from #434:
     the primary document over the official statement over reporting).
  3. GATE — the model only proposes. An NLI model must AGREE with the verdict (evidence entails the
     claim → confirmed; contradicts → refuted); disagreement or no NLI → unresolved, never scored.
  4. EMIT — one ``fact.hindsight`` event per resolved claim, stream-id stable per (source, fact),
     so re-runs re-assert instead of double-counting.

Both prompts are DRAFT (cauri 2026-07-17: ship as DRAFT via /prompts, listed in the PR) and
structured per docs/prompt-template.md.
"""

from __future__ import annotations

import json
import logging

from maat import ids
from maat.pipeline.analyse import judge_entailment_scored
from maat.providers.seam import claude_complete, claude_web_search

log = logging.getLogger("maat.learning.hindsight")

# ⚠️ PROMPT REVIEW (cauri): NEW in-app agent prompt — DRAFT, editable via /prompts.
HINDSIGHT_TRIAGE_PROMPT = r"""# ROLE

You are a claim triager for a news-veracity engine's hindsight review. Given factual claims an
outlet published in the past, you decide which are WORTH resolving against today's knowledge —
and which would be spend without signal.

# GOALS

- Keep only claims that are both FALSIFIABLE and DISCRIMINATING, so the outlet's hindsight record
  measures its reliability rather than its ability to state the obvious.

# INSTRUCTIONS

1. For each numbered claim, answer: could specific evidence that exists TODAY prove this claim
   true or false? (falsifiable)
2. And: would its truth or falsity actually bear on the outlet's reliability? A claim that is true
   by construction — dates, arithmetic, widely-witnessed events nobody disputes — resolves true
   for every outlet and measures nothing. (discriminating)
3. Keep a claim only when BOTH hold.

# GUIDELINES

- Specific, checkable assertions of consequence pass: figures later reported officially, outcomes
  later adjudicated, events later documented, attributions later confirmed or denied.
- Vague characterisations, opinions, predictions, and colour fail — there is nothing to resolve.
- When in doubt, drop it: a smaller, sharper sample beats a padded one.

# GUARDRAILS

- Judge checkability, never truth — resolution happens later, with evidence.
- Do not reward or punish the claim's topic or stance; only whether it can be settled and matters.

# OUTPUT FORMAT

A single JSON array of the claim numbers to KEEP (e.g. [1, 4, 7]) and nothing else. An empty
array is a valid answer.

# CONTEXT

## CLAIMS

{claims}
"""

# ⚠️ PROMPT REVIEW (cauri): NEW in-app agent prompt — DRAFT, editable via /prompts.
HINDSIGHT_RESOLVE_PROMPT = r"""# ROLE

You are a hindsight fact-resolver for a news-veracity engine. Given ONE factual claim an outlet
published in the past, you search for what is known TODAY and report whether the claim proved
true or false — with the most authoritative evidence you can find, quoted word for word.

# GOALS

- Settle the claim with today's knowledge, preferring primary evidence: the official record over
  the press release over the news story about it.

# INSTRUCTIONS

1. Search for what is now known about the claim. Time has passed since it was published — look for
   the official outcome, the final figures, the adjudication, the confirmation or the correction.
2. Prefer the tier ladder: (1) the primary document itself — a peer-reviewed paper, an official
   filing, a court record, the responsible institution's own release or dataset; (2) an official
   statement by the relevant authority; (3) only if neither exists, consistent independent
   reporting from established outlets.
3. Copy ONE sentence VERBATIM from the best page — the exact words, no paraphrase, no ellipsis —
   that settles the claim, whether it confirms or refutes it.
4. Verdict: "true" (the claim held), "false" (it did not), or "unresolved" (today's knowledge
   still cannot settle it). "unresolved" is a correct and common answer — never stretch.

# GUARDRAILS

- Never invent a URL, an institution, or a quote. Every quote must be text you actually read on
  the page at that URL; the engine re-verifies every quote and discards what it cannot check.
- Judge the CLAIM AS PUBLISHED on its publication date — not what later became true of a
  different question, not the outlet's broader coverage.
- Do not let the claim's topic, politics, or the outlet's identity colour the verdict; only the
  evidence speaks.

# OUTPUT FORMAT

A single JSON object and nothing else:
{"outcome": "true" | "false" | "unresolved",
 "url": string, "domain": string, "quote": string, "tier": 1 | 2 | 3}
For "unresolved", url/domain/quote may be empty and tier 3.

# CONTEXT

## THE CLAIM (published {published} by {source})

{claim}
"""

_TRIAGE_MODEL = "claude-sonnet-4-6"   # cauri: in-app assist calls are Sonnet, never Haiku
_RESOLVE_MODEL = "claude-sonnet-4-6"


def triage_claims(texts: list[str], *, prompt: str | None = None) -> list[bool]:
    """Which claims are worth resolving — falsifiable AND discriminating (one batched LLM call).

    Fails CLOSED: on any model/parse trouble every claim is dropped for this batch — hindsight
    spend without the screen would mostly buy mundane-true outcomes, which inflate every outlet
    equally and measure nothing."""
    if not texts:
        return []
    block = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    tmpl = prompt or HINDSIGHT_TRIAGE_PROMPT
    try:
        reply = claude_complete(
            tmpl.replace("{claims}", block), model=_TRIAGE_MODEL, max_tokens=512, stage="hindsight"
        )
        start, end = reply.text.find("["), reply.text.rfind("]")
        keep = {int(x) for x in json.loads(reply.text[start : end + 1])}
    except Exception as e:  # noqa: BLE001
        log.warning("hindsight triage failed (%s) — dropping the batch", type(e).__name__)
        return [False] * len(texts)
    return [(i + 1) in keep for i in range(len(texts))]


def resolve_claim(
    claim: str, *, source: str, published: str, nli, prompt: str | None = None,
) -> dict:
    """Resolve ONE past claim against today's knowledge → the ``fact.hindsight`` payload core.

    The model proposes a verdict + evidence; the NLI model must AGREE (quote entails the claim →
    confirmed, contradicts → refuted) or the outcome is "unresolved". This is the same
    proposes-vs-decides split as every other leg — an LLM's say-so is never a scoring outcome.
    Returns {outcome, evidence:{url, domain, quote, tier}}; outcome ∈ confirmed|refuted|unresolved.
    """
    unresolved = {"outcome": "unresolved", "evidence": {}}
    if nli is None:
        return unresolved  # no gate → no verdict; never trust the search model alone
    tmpl = prompt or HINDSIGHT_RESOLVE_PROMPT
    text = (
        tmpl.replace("{claim}", claim)
        .replace("{source}", source or "unknown")
        .replace("{published}", published or "an earlier date")
    )
    tool = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
    try:
        blocks = claude_web_search(text, tools=[tool], model=_RESOLVE_MODEL, stage="hindsight")
        raw = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start : end + 1])
    except Exception as e:  # noqa: BLE001 - one failed resolution is an unresolved claim, not a crash
        log.warning("hindsight resolve failed (%s) for %r", type(e).__name__, claim[:60])
        return unresolved
    verdict = str(obj.get("outcome") or "").strip().lower()
    quote = str(obj.get("quote") or "").strip()
    url = str(obj.get("url") or "").strip()
    if verdict not in ("true", "false") or not quote or not url:
        return unresolved
    # The NLI agreement gate: evidence must actually SAY what the model concluded.
    label, _strength = judge_entailment_scored(nli, quote, claim)
    if verdict == "true" and label != "entails":
        return unresolved
    if verdict == "false" and label != "contradicts":
        return unresolved
    try:
        tier = int(obj.get("tier"))
    except (TypeError, ValueError):
        tier = 3
    return {
        "outcome": "confirmed" if verdict == "true" else "refuted",
        "evidence": {
            "url": url,
            "domain": str(obj.get("domain") or "").strip(),
            "quote": quote,
            "tier": tier if tier in (1, 2, 3) else 3,
        },
    }


def hindsight_stream_id(source: str, fact: str) -> str:
    """Stable per (source, fact) — a re-run RE-ASSERTS the outcome instead of double-counting.
    (The events log has no dedup; consumers take the LATEST event per stream id.)"""
    return f"hindsight:{source}:{ids.text_fingerprint(fact)[:16]}"


def hindsight_payload(
    *, source: str, fact: str, outcome: str, evidence: dict,
    article_url: str, published_at: str,
) -> dict:
    return {
        "source": source,
        "fact": fact,
        "outcome": outcome,
        "evidence": evidence,
        "article_url": article_url,
        "published_at": published_at,
        "provenance": "hindsight",
    }


def latest_by_stream(rows: list[tuple[str, dict]]) -> list[dict]:
    """Fold raw (stream_id, data) event rows, oldest-first, to the latest payload per stream —
    the re-assert semantics ``hindsight_stream_id`` promises."""
    latest: dict[str, dict] = {}
    for sid, data in rows:
        latest[sid] = data
    return list(latest.values())
