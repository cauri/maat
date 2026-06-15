"""Editable agent prompts, surfaced to the operator console (P8).

The canonical prompt for each stage lives in code (the seed). The console can save audited,
versioned overrides into the ``prompts`` projection; the agents resolve the ACTIVE text at run
time via ``active_text()`` and fall back to the code seed — so an edit takes effect on the next
run and rollback is one click.

Prompt CONTENT is co-designed with cauri. This module only plumbs storage + resolution and a
safety check; it never authors prompt text.
"""

from __future__ import annotations

from maat.pipeline.classify import PROMPT as CLASSIFY_PROMPT
from maat.pipeline.extract import PROMPT as EXTRACT_PROMPT
from maat.pipeline.extremity import PROMPT as EXTREMITY_PROMPT

# key, label, the in-code seed, and the placeholders the template MUST keep (or the run breaks).
PROMPTS: list[dict] = [
    {"key": "extract", "label": "Claim extraction", "default": EXTRACT_PROMPT,
     "placeholders": ["{article_text}", "{source_metadata}", "{detected_language}"]},
    {"key": "classify", "label": "Fact / prediction classifier", "default": CLASSIFY_PROMPT,
     "placeholders": ["{article_text}", "{claims_json}"]},
    {"key": "extremity", "label": "Extraordinary-claim rater", "default": EXTREMITY_PROMPT,
     "placeholders": ["{claim}"]},
]
PROMPTS_BY_KEY: dict[str, dict] = {p["key"]: p for p in PROMPTS}


def seed_default(key: str) -> str:
    """The in-code (canonical) prompt text for a stage. Pure."""
    p = PROMPTS_BY_KEY.get(key)
    return p["default"] if p else ""


def missing_placeholders(key: str, text: str) -> list[str]:
    """Placeholders an edit dropped — the run fills nothing without them, so a save is refused. Pure."""
    p = PROMPTS_BY_KEY.get(key)
    if not p:
        return []
    return [ph for ph in p["placeholders"] if ph not in text]


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
