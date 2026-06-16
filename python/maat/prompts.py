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
from maat.acquire.source_gate import PROMPT as SOURCE_GATE_PROMPT
from maat.serving.topics import _LLM_PROMPT_TEMPLATE as TOPICS_LLM_PROMPT
from maat.serving.topics import NEWS_QUERIES_PROMPT

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


# ---------------------------------------------------------------------------
# The prompt-chat agent's own instructions (#158 / #159) — DRAFT, awaiting cauri review.
#
# DRAFT prompt — flag for cauri review.
#
# This is the system prompt for the console's "Improve with chat" helper: raw Claude (via the
# provider seam) discussing one of Maat's *own* prompts WITH cauri so they can build and improve
# it together. Deliberately minimal — "raw claude will do". At chat time the console substitutes
# {prompt_label} / {prompt_purpose} / {current_prompt} for the prompt under discussion, then the
# running conversation. It is surfaced READ-ONLY in PROMPTS below for review; it never runs the
# pipeline and is not part of EDITABLE_KEYS, so the console can never drift from this code.
# ---------------------------------------------------------------------------
PROMPT_CHAT_AGENT = """You are helping cauri improve one of Maat's own operating prompts, \
through conversation. Maat is a veracity-weighted news platform, and this prompt is the \
instruction text one of its AI steps runs on — so wording precision matters.

The prompt under discussion:
- Name: {prompt_label}
- What it is for: {prompt_purpose}

Its current text is between the fences:
```
{current_prompt}
```

Discuss it plainly with cauri: ask what they want it to do better, point out ambiguity, gaps, \
or risks, and suggest concrete improvements. When — and only when — you are proposing revised \
prompt text, output the COMPLETE new prompt as ONE fenced ``` code block, with nothing but the \
prompt inside that block, so it can drop straight into the editor. Preserve every {placeholder} \
token verbatim: they are filled at run time and the step breaks if one is dropped or renamed. \
Keep all discussion outside the fence. You advise; cauri reviews, applies, and saves the new \
version — you never save anything yourself."""


CONSOLE_ASSISTANT = """ROLE
You are the Maat operator-console assistant. Your role is to help the operator understand the page \
they are on and how the console works, and to point them to the right place to act.

USER ROLE
I am the operator running Maat — sharp, but not an ML engineer. I bring the questions and the \
decisions; you bring clear, concrete explanations.

GOALS
- Explain what the current page is for and what I am looking at.
- Answer my questions about how Maat and the console work, in plain language.
- Point me to the page or control that does what I want.

INSTRUCTIONS
- Answer my question directly and concisely first, then add only the context I need.
- Expand jargon the first time you use it (corroboration, originator, extremity, calibration).
- When I ask you to DO something, say plainly that you cannot act yet, then name the page or \
control that does it.

GUIDELINES
- Prefer short, concrete answers over exhaustive ones.
- If a question needs live data you cannot see, say so and say what you would need.
- If you are unsure, say so rather than guessing.

GUARDRAILS
- Do not invent specific numbers, names, or data you have not been given.
- Do not claim to have taken any action — you have no action tools yet.
- Never present a prediction or an unverified claim as an established fact.

TONE
- Plain, calm, helpful — a knowledgeable colleague, not a manual.
- Short paragraphs or tight lists; skip the preamble.

CONTEXT
WHAT MAAT IS
Maat is a veracity-weighted news system: it reads many sources, extracts claims, classifies fact \
vs prediction, rates how extraordinary each claim is, and corroborates claims across INDEPENDENT \
sources into stories with a confidence score. The console is where the operator observes and \
corrects this engine; every admin action is recorded as an event on an append-only log.

THE CONSOLE'S PAGES
- Feed — the corroborated-stories feed; open a story to see or fix how Maat judged it.
- Activity — what the pipeline has processed, and anything that failed.
- Review — user feedback, triaged.
- Updates — when Maat pulls in new news, with a switch to pause it.
- Settings — the scoring dials (which AI model does each job; the confidence thresholds and \
weights). Changes are recorded as suggestions; applying them to the live engine needs sign-off.
- Policy — what the learning loop would change (bounded, sign-off-gated).
- Prompts — edit the instructions each AI step runs on.
- Sources — every outlet Maat reads and how each is treated (e.g. wire reprints collapsed).
- Reputation — how each source has held up over time.
- Calibration — whether the confidence read is accurate, plus de-US-centering and pipeline health.
- Quality — automatic checks that Maat is still judging correctly.
- Spend — what Maat has spent on AI and acquisition.
- History — a log of every change made in the console.

CURRENT PAGE
{page} — {purpose}"""


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
    {"key": "acquire_queries", "label": "Interest → news search queries", "default": NEWS_QUERIES_PROMPT,
     "status": "active", "source": "maat/serving/topics.py",
     "description": "Turns each reader interest into 2–4 recent-NEWS search queries, so the "
     "acquisition clock fetches news — not evergreen/SEO pages for the literal topic (e.g. 'fun and "
     "laughter' → 'comedy festival', 'feel-good viral story'). Runs once per interest per tick.",
     "placeholders": ["{interest}"]},
    {"key": "source_gate", "label": "Source gate (is this a credible publisher?)",
     "default": SOURCE_GATE_PROMPT, "status": "active", "source": "maat/acquire/source_gate.py",
     "description": "At acquisition, judges each candidate's domain + headline — news outlet / "
     "official primary source / reputable institution are kept; encyclopedias, social, SEO and "
     "content farms are dropped before they ever become an article. Runs once per new domain.",
     "placeholders": ["{domain}", "{headline}", "{channel}"]},
    # --- draft: gated backend prompts, surfaced read-only for cauri review (NOT active) ---
    {"key": "topics_enrich", "label": "NL-interest → acquisition topics (LLM enrichment)",
     "default": TOPICS_LLM_PROMPT, "status": "draft", "source": "maat/serving/topics.py",
     "description": "Would turn a reader's natural-language interest ('West African politics') into "
     "acquisition topics and filters. An optional LLM path — a deterministic keyword extraction "
     "runs today.",
     "placeholders": []},
    {"key": "curation_geotag", "label": "Curation geo-tagger", "default": CURATION_GEOTAG_PROMPT,
     "status": "draft", "source": "maat/agents/curation.py",
     "description": "Would tag a story's primary country/region so curation can balance the feed's "
     "geography and push back on Anglo-American slant. An optional LLM path — pure heuristics run "
     "today.",
     "placeholders": []},
    {"key": "triage_llm", "label": "Feedback-triage refinement", "default": TRIAGE_LLM_PROMPT,
     "status": "draft", "source": "maat/agents/triage.py",
     "description": "Would refine how a piece of user feedback is categorised (veracity-dispute / "
     "source-quality / bug / …) to route it to the review queue or an auto-fix. An optional LLM "
     "path — the rule-based classifier runs today.",
     "placeholders": []},
    {"key": "prompt_chat_agent", "label": "Prompt-chat helper (console)", "default": PROMPT_CHAT_AGENT,
     "status": "draft", "source": "maat/prompts.py",
     "description": "The raw-Claude chat helper on each editable prompt's page — it sees the current "
     "text and discusses improvements with you, proposing a revision you can apply and save. Its own "
     "instructions; surfaced here as a draft for your review (#159), not part of the scored pipeline.",
     "placeholders": []},
    {"key": "console_assistant", "label": "Console assistant (page help)", "default": CONSOLE_ASSISTANT,
     "status": "draft", "source": "maat/prompts.py",
     "description": "The 'Ask Claude about this page' assistant in the console's right panel — it "
     "answers your questions about the current page and how the console works. Surfaced here for "
     "review; {page} and {purpose} are filled with the current page at chat time.",
     "placeholders": ["{page}", "{purpose}"]},
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

# Editable subset — these may be saved, restored, rolled back, or tested. Everything EXCEPT
# on-device: the Apple prompts are Swift mirrors, display-only. Draft prompts are editable like any
# other (#189) — they are LIVE, just tagged "needs review" until the operator clears the tag.
EDITABLE_KEYS: frozenset[str] = frozenset(
    p["key"] for p in PROMPTS if p["status"] != "on-device"
)

# Prompts with a golden eval corpus — only these expose "Test on goldens"; the rest have no fixtures.
GOLDEN_EVAL_KEYS: frozenset[str] = frozenset({"extract", "classify", "extremity"})


def seed_status(key: str) -> str:
    """The in-code status for a prompt (active / draft / on-device). Pure."""
    p = PROMPTS_BY_KEY.get(key)
    return p["status"] if p else ""


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


# ---------------------------------------------------------------------------
# Review tag (#189) — a draft-seed prompt is LIVE like any other, but carries a "needs review"
# marker until the operator clears it. Purely informational: it never gates whether a path runs.
# Persisted as an ``admin.prompt.reviewed`` event read at runtime (like ``clocks.is_paused`` reads
# ``admin.clock.set``) — no kernel projection, no migration.
# ---------------------------------------------------------------------------


def needs_review_given(reviewed_keys: set[str], key: str) -> bool:
    """Pure: does `key` still need review? True when its seed status is "draft" and the operator has
    not yet marked it reviewed (`key` absent from `reviewed_keys`). Active/on-device never need it."""
    return seed_status(key) == "draft" and key not in reviewed_keys


async def review_map(pool) -> dict[str, bool]:
    """``{key: needs_review}`` for every registered prompt, in one pass. A draft-seed prompt needs
    review until an ``admin.prompt.reviewed`` event exists for it. Resilient: no pool / un-migrated
    events table → every draft still needs review (the safe default)."""
    reviewed: set[str] = set()
    if pool is not None:
        try:
            rows = await pool.fetch(
                "select distinct data->>'key' as key from events "
                "where type = 'admin.prompt.reviewed'"
            )
            reviewed = {r["key"] for r in rows if r["key"]}
        except Exception:  # noqa: BLE001 - events table may not exist yet; treat nothing as reviewed
            reviewed = set()
    return {p["key"]: needs_review_given(reviewed, p["key"]) for p in PROMPTS}


async def needs_review(pool, key: str) -> bool:
    """Whether `key` still shows the "needs review" tag — its seed is a draft and no
    ``admin.prompt.reviewed`` event exists for it yet. Resilient (see ``review_map``)."""
    if seed_status(key) != "draft":
        return False
    if pool is None:
        return True
    try:
        row = await pool.fetchrow(
            "select 1 from events where type = 'admin.prompt.reviewed' and data->>'key' = $1 limit 1",
            key,
        )
    except Exception:  # noqa: BLE001 - events table may not exist yet; still needs review
        return True
    return row is None
