"""Primary-source grounding judge (#228, §5) — does the primary source actually back the claim?

When ``MAAT_GROUNDING_LLM=1`` the grounding agent (``agents.grounding_agent``) uses this to judge
each primary-bearing cluster's fact against its primary source's text: SUPPORTED / CONTRADICTED /
NOT_ADDRESSED. The verdict refines the confidence read (the primary lift is earned only on genuine
support) and feeds the refutation path (a primary that contradicts the claim → REFUTED). One
Sonnet call per primary-bearing cluster; returns "" on anything uncertain so the caller leaves the
cluster ungrounded (confidence unchanged).

The prompt below follows cauri's template and is the operator-editable ``grounding`` seed in
``maat.prompts`` (gated OFF until MAAT_GROUNDING_LLM=1).
"""

from __future__ import annotations

import json

from maat.providers.seam import claude_complete

# cauri: Sonnet, not the cheap tier — quality over cost. Gated to primary-bearing clusters only.
GROUNDING_MODEL = "claude-sonnet-4-6"

_VERDICTS = {
    "SUPPORTED": "supported",
    "CONTRADICTED": "contradicted",
    "NOT_ADDRESSED": "not_addressed",
}

# In-platform agent prompt (cauri-approved, #228). Runtime variables under CONTEXT. Mirrored as the
# `grounding` seed in maat.prompts; operator-editable via the console.
GROUNDING_PROMPT = """ROLE
You are a primary-source verification specialist. Your role is to judge whether a news claim is
supported by the primary/authoritative document it should derive from — measured against that
source, never against consensus or outside reporting.

GOALS
- Decide if the PRIMARY SOURCE supports, contradicts, or does not address the CLAIM.
- Reward only genuine support; expose claims a primary source is cited for but doesn't back.
- Surface direct contradiction as a strong falsity signal.

PROCESS
1. Identify the CLAIM's checkable assertion (the who / what / number / date).
2. Search the PRIMARY SOURCE for that specific assertion.
3. Decide SUPPORTED (the source states or directly entails it), CONTRADICTED (the source asserts
   something incompatible), or NOT_ADDRESSED (silent, tangential, or ambiguous).

GUIDELINES
- Judge ONLY against the provided source text — not prior knowledge or other reporting.
- Topical overlap is NOT support; require the source to carry the claim's specific assertion.
- A matching figure/paraphrase is SUPPORTED; a different figure/date/actor is CONTRADICTED.
- Prefer NOT_ADDRESSED over a guess when the source is silent or partial.

GUARDRAILS
- Do not infer beyond what the source states; introduce no outside facts.
- Never return SUPPORTED on similarity alone.
- If the source text is empty or clearly not a primary record, return NOT_ADDRESSED.

OUTPUT FORMAT
- JSON only: {{"verdict":"SUPPORTED|CONTRADICTED|NOT_ADDRESSED","evidence":"<=160-char span from the source, or empty"}}

CONTEXT
CLAIM
{fact}

PRIMARY SOURCE ({source_name})
{primary_body}
"""


def judge_grounding(
    fact: str, source_name: str, primary_body: str, *, prompt: str | None = None
) -> tuple[str, str]:
    """Judge whether `primary_body` supports `fact`. Returns (verdict, evidence).

    verdict ∈ {"supported","contradicted","not_addressed"}, or "" on empty input / error /
    unparseable reply — the caller then leaves the cluster ungrounded (confidence unchanged).
    """
    if not fact.strip() or not primary_body.strip():
        return "", ""
    tmpl = prompt or GROUNDING_PROMPT
    try:
        reply = claude_complete(
            tmpl.format(
                fact=fact[:2000],
                source_name=source_name or "primary source",
                primary_body=primary_body[:8000],
            ),
            model=GROUNDING_MODEL,
            stage="grounding",
        )
        raw = reply.text
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        verdict = _VERDICTS.get(str(data.get("verdict", "")).strip().upper(), "")
        evidence = str(data.get("evidence", "")).strip()[:160]
        return verdict, evidence
    except Exception:
        return "", ""
