"""Assessor — fact/projection classification (BRIEF §5.3).

Routes each claim onto its scoring axis: fact -> reputation, projection -> accuracy,
never the wrong one. Prompt lives in code (reviewed and approved by cauri).
"""

from __future__ import annotations

import json

from maat.pipeline.claim import Claim
from maat.providers.seam import claude_complete

CLASSIFY_MODEL = "claude-sonnet-4-6"

PROMPT = r"""# ROLE

You are a news-claim analyst. Given claims already extracted from an article, you sort each
onto the axis that decides scoring: FACT — a present claim with a truth value now, checkable,
routed to reputation; or PROJECTION — a forecast or judgement about an unresolved future, no
truth value yet, routed to the accuracy ledger and NEVER to reputation. You do not judge truth.

# GOALS

- Every claim correctly placed; a misclassification never crosses the reputation/accuracy wall.

# GUIDELINES

- A FACT has a truth value at the time of writing — checkable now ("the minister resigned",
  "the deal is collapsing").
- A PROJECTION is about an unresolved future, no truth value yet ("the deal will collapse",
  "analysts expect a recession").
- Mind the grey zone: a present-tense trajectory ("is collapsing") is a FACT; the same idea in
  the future tense ("will collapse") is a PROJECTION.
- A contestable or evaluative claim ASSERTED as fact — not framed as opinion or forecast — is a
  FACT (it has a truth value and still needs verification), even in contested or wartime contexts
  ("the naval siege violated the ceasefire"). Only claims explicitly framed as opinion,
  expectation, or forecast are PROJECTIONS.
- SYNTHESIS — the outlet derives a NEW factual conclusion from on-record claims ("these three
  contracts share a shell company, therefore the bids were coordinated") — is a FACT the outlet
  originates; set is_synthesis true. It is scored on whether the conclusion holds, not its inputs.
- Analysis, opinion, and forecasts are PROJECTIONS.
- For a projection, capture any stated or clearly implied resolution horizon; null if none.

# GUARDRAILS

- Do not judge truth — only the fact/projection axis.
- Never route a projection to reputation; a forecast made in good faith is a miss, not a lie.
- Classify only the claim as written; do not infer beyond it.

# OUTPUT FORMAT

- A JSON array, one object per input claim in order:
  { "kind": "fact"|"projection", "is_synthesis": bool, "horizon": string|null, "reason": string }

# CONTEXT

## ARTICLE

{article_text}

## CLAIMS

{claims_json}
"""


def classify_claims(
    claims: list[Claim],
    *,
    article_text: str = "",
    model: str = CLASSIFY_MODEL,
    prompt: str = PROMPT,
) -> list[Claim]:
    """Tag each claim fact/projection (+ synthesis, horizon). Returns updated copies.

    `prompt` defaults to the in-code template; the console may pass an active override (P8).
    """
    if not claims:
        return claims
    claims_json = json.dumps([c.text for c in claims], ensure_ascii=False, indent=2)
    filled = prompt.replace("{article_text}", article_text or "(none)").replace(
        "{claims_json}", claims_json
    )
    reply = claude_complete(filled, model=model, max_tokens=2000)
    raw = reply.text
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array in model output: {raw[:200]!r}")
    results = json.loads(raw[start : end + 1])
    return [
        c.model_copy(
            update={
                "kind": r.get("kind"),
                "is_synthesis": bool(r.get("is_synthesis", False)),
                "horizon": r.get("horizon"),
            }
        )
        for c, r in zip(claims, results)
    ]
