"""Agent prompts, surfaced to the operator console (P8).

Three kinds of prompt are registered here so cauri can review *every* prompt the platform runs:

- ``active``    — the editable backend prompts. The canonical text lives in code (the seed); the
                  console can save audited, versioned overrides into the ``prompts`` projection and
                  the agents resolve the ACTIVE text at run time via ``active_text()``, falling back
                  to the seed — so an edit takes effect on the next run and rollback is one click.
- ``draft``     — backend prompts that exist in code but are GATED off (the LLM path is disabled by
                  default). They are surfaced READ-ONLY for cauri review; they are NOT editable and
                  NOT resolved by ``active_text``. Their text is imported LIVE from the owning module
                  so the console can never drift from the code.
- ``on-device`` — the Apple / Foundation Models prompts that run on the reader's phone. Swift cannot
                  be imported, so these are mirrored here as display-only text. They are READ-ONLY.

Every entry carries a ``status`` (one of the three above) and a ``source`` (the file the canonical
text lives in). Only ``active`` entries have ``placeholders``/edit behaviour.

Prompt CONTENT is co-designed with cauri. This module only plumbs storage + resolution, a safety
check, and surfacing; it never authors prompt text.
"""

from __future__ import annotations

from maat.agents.curation import _DRAFT_GEOTAG_PROMPT as CURATION_GEOTAG_PROMPT
from maat.agents.triage import TRIAGE_LLM_PROMPT
from maat.pipeline.classify import PROMPT as CLASSIFY_PROMPT
from maat.pipeline.extract import PROMPT as EXTRACT_PROMPT
from maat.pipeline.extremity import PROMPT as EXTREMITY_PROMPT
from maat.serving.topics import _LLM_PROMPT_TEMPLATE as TOPICS_LLM_PROMPT

# ---------------------------------------------------------------------------
# On-device (Apple / Foundation Models) prompts — DISPLAY-ONLY MIRRORS.
#
# Swift cannot be imported, so these mirror the text of the `instructions:` and `prompt` blocks in
# the named Swift files VERBATIM (the runtime-dedented content the model receives). They are kept
# in sync with the Swift source by ``tests/test_prompts_ondevice_mirror.py``, which reads the Swift
# files and asserts these strings match byte-for-byte. Raw strings keep Swift `\(…)` interpolation
# markers literal. DO NOT edit the prompt text here — edit the Swift source and update the mirror.
# ---------------------------------------------------------------------------

# apple/Maat/Shared/Summarizer.swift — `instructions:` block then `prompt` block.
_SUMMARIZER_ONDEVICE = (
    "instructions:\n"
    r"""You summarise one news story for a reader in at most two sentences.
Use only the claims given. Do not add facts. Preserve whether each claim is stated in the
outlet's own voice or attributed to someone, and never overstate how confirmed it is."""
    "\n\nprompt:\n"
    r"""Story: \(story.fact)
Claims:
\(claims)"""
)

# apple/Maat/Shared/Reranker.swift — `instructions:` block then `prompt` block.
_RERANKER_ONDEVICE = (
    "instructions:\n"
    r"""You re-rank a personal news feed for one reader against their topics of interest.
Judge relevance only — never judge whether a story is true, and never drop a story."""
    "\n\nprompt:\n"
    r"""Reader topics: \(topics.joined(separator: ", "))

Stories (id: claim):
\(lines)

Order every id from most to least relevant to the reader's topics."""
)


# key, label, the in-code seed, status, source, and (for editable prompts) the placeholders the
# template MUST keep (or the run breaks).
PROMPTS: list[dict] = [
    # --- active: editable backend prompts (canonical text in code; overrides go live next run) ---
    {"key": "extract", "label": "Claim extraction", "default": EXTRACT_PROMPT,
     "status": "active", "source": "maat/pipeline/extract.py",
     "description": "Pulls the atomic factual claims out of each article as it is ingested — the "
     "claim, not the article, is what everything downstream scores. Runs once per article.",
     "placeholders": ["{article_text}", "{source_metadata}", "{detected_language}"]},
    {"key": "classify", "label": "Fact / prediction classifier", "default": CLASSIFY_PROMPT,
     "status": "active", "source": "maat/pipeline/classify.py",
     "description": "Sorts each extracted claim onto its axis — a present-tense fact (corroborated "
     "against independent sources) vs a projection/prediction (tracked on the accuracy axis) — so "
     "neither is scored the wrong way. Runs per article after extraction.",
     "placeholders": ["{article_text}", "{claims_json}"]},
    {"key": "extremity", "label": "Extraordinary-claim rater", "default": EXTREMITY_PROMPT,
     "status": "active", "source": "maat/pipeline/extremity.py",
     "description": "Rates how extraordinary a claim is, before any evidence, on a 5-point "
     "routine→extraordinary scale; the confidence read uses it to demand more independent "
     "corroboration for bigger claims. Runs per fact.",
     "placeholders": ["{claim}"]},
    # --- draft: gated backend prompts, surfaced read-only for cauri review (NOT active) ---
    {"key": "topics_enrich", "label": "NL-interest → acquisition topics (LLM enrichment)",
     "default": TOPICS_LLM_PROMPT, "status": "draft", "source": "maat/serving/topics.py",
     "description": "Would turn a reader's natural-language interest ('West African politics') into "
     "acquisition topics and filters. Gated OFF — deterministic keyword extraction runs today; the "
     "model path awaits your review.",
     "placeholders": []},
    {"key": "curation_geotag", "label": "Curation geo-tagger", "default": CURATION_GEOTAG_PROMPT,
     "status": "draft", "source": "maat/agents/curation.py",
     "description": "Would tag a story's primary country/region so curation can balance the feed's "
     "geography and push back on Anglo-American slant. Gated OFF — pure heuristics run today; the "
     "model path awaits your review.",
     "placeholders": []},
    {"key": "triage_llm", "label": "Feedback-triage refinement", "default": TRIAGE_LLM_PROMPT,
     "status": "draft", "source": "maat/agents/triage.py",
     "description": "Would refine how a piece of user feedback is categorised (veracity-dispute / "
     "source-quality / bug / …) to route it to the review queue or an auto-fix. Gated OFF — the "
     "rule-based classifier runs today; the model path awaits your review.",
     "placeholders": []},
    # --- on-device: Apple / Foundation Models prompts, display-only mirror (READ-ONLY) ---
    {"key": "summarizer_ondevice", "label": "On-device summariser (Foundation Models)",
     "default": _SUMMARIZER_ONDEVICE, "status": "on-device",
     "source": "apple/Maat/Shared/Summarizer.swift",
     "description": "Summarises a story to the reader's taste on their own device (Apple Foundation "
     "Models), built only from the story's own claims so it never adds facts or inflates "
     "confidence. Falls back to a deterministic extractive summary.",
     "placeholders": []},
    {"key": "reranker_ondevice", "label": "On-device feed re-ranker (Foundation Models)",
     "default": _RERANKER_ONDEVICE, "status": "on-device",
     "source": "apple/Maat/Shared/Reranker.swift",
     "description": "Re-orders the reader's feed against their natural-language topics on their own "
     "device (Apple Foundation Models) — relevance only, never touching confidence or truth, and "
     "topics never leave the phone. Falls back to embedding similarity.",
     "placeholders": []},
]
PROMPTS_BY_KEY: dict[str, dict] = {p["key"]: p for p in PROMPTS}

# The editable subset — only these may be saved, restored, rolled back, or tested. Draft and
# on-device prompts are surfaced for review but never become a live override.
EDITABLE_KEYS: frozenset[str] = frozenset(
    p["key"] for p in PROMPTS if p["status"] == "active"
)


def seed_default(key: str) -> str:
    """The in-code (canonical) prompt text for a stage. Pure."""
    p = PROMPTS_BY_KEY.get(key)
    return p["default"] if p else ""


def missing_placeholders(key: str, text: str) -> list[str]:
    """Placeholders an edit dropped — the run fills nothing without them, so a save is refused. Pure."""
    p = PROMPTS_BY_KEY.get(key)
    if not p:
        return []
    return [ph for ph in p.get("placeholders", []) if ph not in text]


async def active_text(pool, key: str, default: str) -> str:
    """The active prompt text for `key` from the store, else the code seed. Resilient: any
    problem (no pool, table not migrated yet) falls back to the seed so the pipeline never stalls.
    """
    if pool is None:
        return default
    try:
        row = await pool.fetchrow(
            "select text from prompts where key = $1 and active order by version desc limit 1", key
        )
    except Exception:  # noqa: BLE001 - prompts table may not exist yet; fall back to the seed
        return default
    return row["text"] if row and row["text"] else default
