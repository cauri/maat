"""Extremity — the prior against a claim (BRIEF §5.6).

"Extraordinary claims require extraordinary evidence." The confidence read scales the
corroboration bar by how extraordinary a fact is: an ordinary claim is believable on thin
support, an extraordinary one needs many independent originators to reach the same
confidence. This rates the PRIOR only — never the verdict; a claim can be extraordinary
and true, or ordinary and false.

DRAFT — created during the away-build; cauri reviewed it and moved it to a 5-point scale on
Sonnet, with the bar set a bit higher (the decay constants in `corroborate.confidence_read`).
Prompt boundaries flagged for cauri's follow-up review.
"""

from __future__ import annotations

import json

from maat.providers.seam import claude_complete

EXTREMITY_MODEL = "claude-sonnet-4-6"  # a 5-point prior judgement (cauri: Sonnet for sharper priors)

LEVELS = ("routine", "ordinary", "notable", "significant", "extraordinary")

PROMPT = r"""# ROLE

You rate the PRIOR against a single factual claim — how surprising it would be if asserted
without support — on a FIVE-POINT scale, so a downstream confidence read can demand more
corroboration for the more extraordinary. You are NOT judging whether the claim is true; only
how surprising it would be before any evidence.

# GOALS

- Place the claim on a five-level prior, judged before any corroboration is seen.

# GUIDELINES (lowest to highest prior-implausibility)

- routine — happens constantly and is near-always reported accurately; nothing rides on it being
  wrong ("markets opened", "the council met", "the index closed lower").
- ordinary — normal hard news, an expected kind of event for its context ("a minister gave a
  policy speech", "the central bank held rates", "a company reported quarterly earnings").
- notable — consequential or mildly surprising, but unremarkable that it happened ("a minister
  resigned", "merger talks collapsed", "a factory closed with job losses").
- significant — surprising and serious, or contested; a reasonable reader would want it
  well-sourced ("a minister resigned amid a corruption probe", "a breach exposed millions of
  records", "a ceasefire was violated").
- extraordinary — if true it overturns expectations or carries grave consequence, or it is
  inherently rare and hard to establish ("a minister ordered killings", "an election was rigged",
  "a banned weapon was used", "a leader secretly diverted state funds").

# GUARDRAILS

- Judge the prior only, never the truth: a claim can be extraordinary and true, or routine and false.
- Rate the claim as written; do not import outside knowledge about the specific people or bodies named.
- When genuinely between two levels, choose the LOWER — never inflate the bar.

# OUTPUT FORMAT

- A JSON object:
  { "extremity": "routine"|"ordinary"|"notable"|"significant"|"extraordinary", "reason": string }

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


def rate_extremity(fact: str, *, model: str = EXTREMITY_MODEL, prompt: str = PROMPT) -> str:
    """Rate a fact's prior: ordinary | notable | extraordinary (§5.6).

    `prompt` defaults to the in-code template; the console may pass an active override (P8).
    """
    reply = claude_complete(prompt.replace("{claim}", fact), model=model, max_tokens=200,
                            stage="extremity")
    return _parse_extremity(reply.text)
