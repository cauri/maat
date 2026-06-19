"""DRAFT LLM geo-tagging for curation (#189, P6) — review with cauri before enabling.

When ``MAAT_CURATION_LLM=1`` the geotag agent (``agents.geotag_agent``) uses this to infer the
primary country a story is ABOUT from its fact text — the de-US re-ranker's gap-filler for the
clusters the TLD/language heuristic (``serving.feed._infer_country``) cannot place (e.g.
English-language wire copy about a non-Anglophone event). One bulk-model call per unplaced cluster;
returns "" on anything uncertain so curation falls back to treating the country as unknown
(uncapped) rather than guessing.

DRAFT — the prompt below is a first cut. On review, move it into the operator prompt store (there
is already a ``curation_geotag`` seed in ``maat.prompts``) and apply cauri's prompt template before
turning the flag on in prod.
"""

from __future__ import annotations

import json
import re

from maat.providers.seam import claude_complete

# cauri: Sonnet, not the cheap tier — quality over cost. Called only for clusters the heuristic
# can't place (gated by MAAT_CURATION_LLM), so the volume is the ambiguous tail, not the corpus.
GEOTAG_MODEL = "claude-sonnet-4-6"

_ISO2 = re.compile(r"^[A-Z]{2}$")

# DRAFT — review with cauri (in-platform agent prompt fed to the bulk model; see D22/D23).
_GEOTAG_PROMPT = (
    "What ONE country is the news story below primarily ABOUT — where the event happens or whose "
    "institutions/people it concerns? Answer with that country's ISO-3166-1 alpha-2 code (e.g. US, "
    "FR, NG, BR). If the story is genuinely global, or you cannot tell, answer XX. Return ONLY a "
    'JSON object: {{"country": "<code>"}}. No prose.\n\nSTORY:\n{text}\n'
)


def llm_country(text: str) -> str:
    """Infer an ISO-3166-1 alpha-2 country for a story via the bulk model.

    Returns the uppercased 2-letter code, or "" when the story is global/ambiguous or anything
    goes wrong (bad JSON, transport error) — the caller treats "" as "leave it to the heuristic".
    """
    if not text.strip():
        return ""
    try:
        # Sonnet (cauri): one call per UNPLACED cluster per tick — the agent only reaches here for
        # clusters the heuristic already failed to place, so the volume stays bounded.
        reply = claude_complete(_GEOTAG_PROMPT.format(text=text[:2000]), model=GEOTAG_MODEL,
                                stage="geotag")
        raw = reply.text
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
        code = str(data.get("country", "")).strip().upper()
        if code and code != "XX" and _ISO2.match(code):
            return code
    except Exception:
        pass
    return ""
