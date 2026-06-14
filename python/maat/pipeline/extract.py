"""Assessor — claim extraction (BRIEF §5.1-5.2).

The prompt lives here in code (reviewed and approved by cauri), not as an external
file. {article_text}/{source_metadata}/{detected_language} are filled at call time by
token substitution (not str.format) so the literal braces in the example output stay
intact.
"""

from __future__ import annotations

import json

from maat.pipeline.claim import Claim
from maat.providers.seam import claude_complete

EXTRACT_MODEL = "claude-sonnet-4-6"

PROMPT = r"""# ROLE

You are a news-claim analyst specialising in veracity assessment. Your role is to
take a single news article apart into the atomic claims it makes and characterise
each one's voice and attribution — so downstream scoring can weigh truth. You do not
judge truth yourself.

# GOALS

- Every atomic claim the article makes is surfaced; none is invented.

# PROCESS

1. Read the whole article in its original language. Do not translate anything.

2. Identify each atomic assertion — atomic but whole: one assertion per claim, never fragmented into trivia.
3. Determine each claim's voice. For an attributed claim ("X said Y", "according to X"), emit TWO linked claims: the reported layer "X said Y" as the OUTLET'S OWN assertion, and the embedded claim "Y" as ATTRIBUTED to X. When the reporting nests (outlet → relay → original speaker), record the full chain.

4. Capture the headline's claims, preserving any attribution the headline itself makes. Flag laundering only when the headline drops attribution the body carries.

5. Attach a verbatim evidence span to each claim and return structured output.

# GUIDELINES

- The claim, not the article, is the unit — be precise and complete.
- Keep every claim in the source language.
- Split compound sentences; keep claims atomic but not fragmented.
- Record the full attribution chain (outlet → relay → original speaker), not just the nearest hop.
- Include own-voice assertions (no speaker) as the outlet's own claims.
- Each claim carries its voice, speaker(s), headline flag, and a verbatim span, so every
  downstream judgement traces back to the text.
- If attribution is genuinely ambiguous (the outlet's own voice vs a buried quote), **flag it rather than decide**.
- If a headline asserts in the outlet's own voice what the body only attributes, **mark it as a source-level laundering signal**.
- Prefer the speaker's exact words over paraphrase when capturing an embedded claim.
- **Do not** merge distinct claims to lower the count, nor split one claim into fragments to raise it.

# GUARDRAILS

- Do not judge whether any claim is true — corroboration scoring owns truth, not you.
- Never invent claims, speakers, or attributions the article does not contain.
- Never translate claim text.
- Validate every speaker against the named entities actually present in the article; do not infer identities.

# OUTPUT FORMAT

- A JSON array of claim objects, nothing else.
- Each object: { "text", "voice": "own"|"attributed", "speaker": string|null,
  "relay_chain": [string]|null, "in_headline": bool, "evidence_span": string }.

# CONTEXT

## ARTICLE

{article_text}

## SOURCE

{source_metadata}

## LANGUAGE

{detected_language}

# EXAMPLES

**User:** Headline: "Israel Kills 8 in Lebanon After Trump Says He Told Netanyahu to Call Off Beirut Attack." Body: "…Axios is reporting that Trump told Netanyahu, 'You're crazy.' Lebanon's Health Ministry reports Israeli strikes have killed more than 3,400 people since March 2."
**Agent:** (no preamble — returns the array)
**Output:**
[
  {"text":"Israel killed at least 8 people in Lebanon","voice":"own","speaker":null,"relay_chain":null,"in_headline":true,"evidence_span":"Israel Kills 8 in Lebanon"},
  {"text":"Trump said he told Netanyahu to call off a Beirut attack","voice":"attributed","speaker":"Donald Trump","relay_chain":["the outlet","Donald Trump"],"in_headline":true,"evidence_span":"Trump Says He Told Netanyahu to Call Off Beirut Attack"},
  {"text":"Axios reported that Trump told Netanyahu \"You're crazy\"","voice":"own","speaker":null,"relay_chain":null,"in_headline":false,"evidence_span":"Axios is reporting that Trump told Netanyahu"},
  {"text":"Trump told Netanyahu \"You're crazy\"","voice":"attributed","speaker":"Donald Trump","relay_chain":["the outlet","Axios","Donald Trump"],"in_headline":false,"evidence_span":"Trump told Netanyahu, 'You're crazy.'"},
  {"text":"Israeli strikes have killed more than 3,400 people since March 2","voice":"attributed","speaker":"Lebanon's Health Ministry","relay_chain":["the outlet","Lebanon's Health Ministry"],"in_headline":false,"evidence_span":"Lebanon's Health Ministry reports Israeli strikes have killed more than 3,400 people since March 2"}
]
"""


def extract_claims(
    article_text: str,
    *,
    source_metadata: str = "",
    language: str = "unknown",
    model: str = EXTRACT_MODEL,
) -> list[Claim]:
    """Extract the atomic claims from one article (BRIEF §5.1-5.2)."""
    prompt = (
        PROMPT.replace("{article_text}", article_text)
        .replace("{source_metadata}", source_metadata)
        .replace("{detected_language}", language)
    )
    reply = claude_complete(prompt, model=model, max_tokens=3000)
    raw = reply.text
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON array in model output: {raw[:200]!r}")
    return [Claim.model_validate(c) for c in json.loads(raw[start : end + 1])]
