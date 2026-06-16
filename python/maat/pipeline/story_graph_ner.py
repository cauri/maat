"""DRAFT LLM entity extraction for the story graph (#42) — review with cauri before enabling.

When ``MAAT_STORY_GRAPH_LLM=1`` the builder uses this instead of the proper-noun heuristic
(``story_graph_build.entity_spine_heuristic``): it asks Claude for the canonical
persons/organisations/places that name an event — a cleaner entity spine for attachment — at one
LLM call per cluster, falling back to the heuristic on any error.

DRAFT — the prompt below is a first cut; on review, move it into the operator prompt store
(maat.prompts) and apply cauri's prompt template before turning the flag on in prod.
"""

from __future__ import annotations

import json

from maat.providers.seam import claude_complete

# DRAFT — review with cauri (in-platform agent prompt fed to Claude; see D22/D23).
_NER_PROMPT = (
    "Extract the canonical real-world ENTITIES that identify the news event described below — "
    "the specific people, organisations, and places it is ABOUT. Exclude generic nouns, dates, "
    "and the reporting outlet. Return ONLY a JSON array of lowercase strings, most specific "
    "first, at most {max_entities}. No prose.\n\nEVENT TEXT:\n{text}\n"
)


def llm_entity_spine(text: str, *, max_entities: int = 8) -> list[str]:
    """Canonical entity ids via the LLM; falls back to the heuristic on any error."""
    if not text.strip():
        return []
    try:
        reply = claude_complete(_NER_PROMPT.format(max_entities=max_entities, text=text[:2000]))
        raw = reply.text
        arr = json.loads(raw[raw.find("[") : raw.rfind("]") + 1])
        out: list[str] = []
        seen: set[str] = set()
        for t in arr:
            v = str(t).strip().lower()
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        return out[:max_entities]
    except Exception:
        from maat.pipeline.story_graph_build import entity_spine_heuristic

        return entity_spine_heuristic(text, max_entities=max_entities)
