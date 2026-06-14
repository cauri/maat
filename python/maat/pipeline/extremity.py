"""Extremity — the prior against a claim (BRIEF §5.6).

"Extraordinary claims require extraordinary evidence." The confidence read scales the
corroboration bar by how extraordinary a fact is: an ordinary claim is believable on thin
support, an extraordinary one needs many independent originators to reach the same
confidence. This rates the PRIOR only — never the verdict; a claim can be extraordinary
and true, or ordinary and false.

DRAFT — new prompt, created during the away-build for review on return. The three levels
and the decay they map to (in `corroborate.confidence_read`) are first cuts.
"""

from __future__ import annotations

import json

from maat.providers.seam import claude_complete

EXTREMITY_MODEL = "claude-haiku-4-5-20251001"  # a 3-way prior judgement; cheap is fine

LEVELS = ("ordinary", "notable", "extraordinary")

PROMPT = r"""# ROLE

You rate the PRIOR against a single factual claim — how extraordinary it would be if asserted
without support — so a downstream confidence read can demand more corroboration for the
extraordinary. You are NOT judging whether the claim is true; only how surprising it is.

# GOALS

- Place the claim on a three-level prior, judged before any corroboration is seen.

# GUIDELINES

- ordinary — routine and expected, low-stakes, the kind of thing reported daily and seldom wrong
  ("the minister attended the summit", "the index closed lower").
- notable — consequential or contested but unsurprising in its context; a normal hard-news claim
  ("the minister resigned amid a procurement scandal", "the ceasefire talks collapsed").
- extraordinary — if true it overturns expectations or carries grave consequence, or it is
  inherently rare and hard to establish ("the minister ordered the killings", "the vote was
  rigged", "a banned weapon was used"). Judge by the prior, not by your guess at the verdict.

# GUARDRAILS

- Judge the prior only, never the truth: a claim can be extraordinary and true, or ordinary and false.
- Rate the claim as written; do not import outside knowledge about the specific people or bodies named.
- When genuinely unsure between two levels, choose the lower (do not inflate the bar).

# OUTPUT FORMAT

- A JSON object: { "extremity": "ordinary"|"notable"|"extraordinary", "reason": string }

# CONTEXT

## CLAIM

{claim}
"""


def _parse_extremity(raw: str) -> str:
    """Pull the level out of model output; default to `notable` (neither penalise nor reward)."""
    s, e = raw.find("{"), raw.rfind("}")
    if s == -1 or e == -1:
        return "notable"
    try:
        obj = json.loads(raw[s : e + 1])
    except json.JSONDecodeError:
        return "notable"
    val = obj.get("extremity")
    return val if val in LEVELS else "notable"


def rate_extremity(fact: str, *, model: str = EXTREMITY_MODEL) -> str:
    """Rate a fact's prior: ordinary | notable | extraordinary (§5.6)."""
    reply = claude_complete(PROMPT.replace("{claim}", fact), model=model, max_tokens=200)
    return _parse_extremity(reply.text)
