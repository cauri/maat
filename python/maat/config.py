"""Config registry for the operator console (P8, F5).

The tunable knobs of the veracity pipeline — model routing and the scoring thresholds —
surfaced so the operator can SEE them (today they are buried as constants) and PROPOSE
changes. A proposal is recorded as an ``admin.threshold.changed`` event: audited and
versioned in the log. It is **not** auto-applied.

Veracity-core knobs (gate floor, scoring, the judge/classifier models) are marked ``core``:
promoting a proposal into the live pipeline needs explicit sign-off and an A/B-on-replay pass
(D18 / §5) — that promotion path is a deliberate follow-up, not wired here. Defaults are read
from the live code so the view never drifts from what the pipeline actually uses.
"""

from __future__ import annotations

import json

from maat.pipeline.classify import CLASSIFY_MODEL
from maat.pipeline.corroborate import (
    _CONFIDENCE_CAP,
    _DECAY,
    _PRIMARY_LIFT,
    _W_ANONYMOUS,
    _W_BALD,
    _W_NAMED,
)
from maat.pipeline.extremity import EXTREMITY_MODEL
from maat.providers.seam import CLAUDE_JUDGE, MISTRAL_BULK, MISTRAL_EMBED

# Selectable model options for the model-routing dropdowns (the current value is always added if
# missing). Claude family for judgement stages; Mistral for bulk + embeddings.
_CLAUDE_MODELS = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001", "claude-fable-5"]
_MISTRAL_CHAT = ["mistral-large-latest", "mistral-medium-latest", "mistral-small-latest",
                 "ministral-8b-latest", "ministral-3b-latest"]
_MISTRAL_EMBED = ["mistral-embed"]

# Each knob: key, human label, group, current default (from live code), core?, code source, plus
# `type` (model | float | int) for the right input control, `help` (plain-language tooltip), and
# `options` (model dropdowns). The threshold/clustering literals live inside functions (not
# importable constants), so they are mirrored here with their source — promotion would lift them
# to a single config read.
KNOBS: list[dict] = [
    {"key": "model.judge", "label": "Judge model", "group": "Model routing", "type": "model",
     "options": _CLAUDE_MODELS, "default": CLAUDE_JUDGE, "core": True,
     "source": "providers/seam.py:CLAUDE_JUDGE",
     "help": "The Claude model used for the careful judgement calls. Pricier models judge better but cost more per article."},
    {"key": "model.classify", "label": "Fact / prediction classifier", "group": "Model routing",
     "type": "model", "options": _CLAUDE_MODELS, "default": CLASSIFY_MODEL, "core": True,
     "source": "pipeline/classify.py:CLASSIFY_MODEL",
     "help": "The model that decides whether each sentence is a fact or a prediction."},
    {"key": "model.extremity", "label": "Extraordinary-claim rater", "group": "Model routing",
     "type": "model", "options": _CLAUDE_MODELS, "default": EXTREMITY_MODEL, "core": False,
     "source": "pipeline/extremity.py:EXTREMITY_MODEL",
     "help": "The model that rates how extraordinary a claim is, so bigger claims are made to earn more corroboration."},
    {"key": "model.bulk", "label": "Bulk model", "group": "Model routing", "type": "model",
     "options": _MISTRAL_CHAT, "default": MISTRAL_BULK, "core": False,
     "source": "providers/seam.py:MISTRAL_BULK",
     "help": "The cheap, fast Mistral model used for high-volume background work."},
    {"key": "model.embed", "label": "Embedding model", "group": "Model routing", "type": "model",
     "options": _MISTRAL_EMBED, "default": MISTRAL_EMBED, "core": False,
     "source": "providers/seam.py:MISTRAL_EMBED",
     "help": "Turns text into vectors so near-identical claims can be grouped into one story."},
    {"key": "gate.floor", "label": "Hide stories below", "group": "Veracity thresholds (§5.7)",
     "type": "float", "default": "0.40", "core": True, "source": "corroborate.py:confidence_label",
     "help": "Stories below this confidence are hidden as too thinly sourced. Lower shows more (including weaker stories); higher is stricter."},
    {"key": "tier.corroborated", "label": "'Corroborated' label at", "group": "Veracity thresholds (§5.7)",
     "type": "float", "default": "0.60", "core": True, "source": "corroborate.py:confidence_label",
     "help": "The confidence at which a story earns the 'Corroborated' label."},
    {"key": "tier.well", "label": "'Well corroborated' label at", "group": "Veracity thresholds (§5.7)",
     "type": "float", "default": "0.85", "core": True, "source": "corroborate.py:confidence_label",
     "help": "The confidence at which a story earns the strongest 'Well corroborated' label."},
    {"key": "decay.routine", "label": "Corroboration value · routine claim", "group": "Extremity (§5.6)",
     "type": "float", "default": str(_DECAY["routine"]), "core": True, "source": "corroborate.py:_DECAY",
     "help": "For an ordinary, expected claim: how much each extra independent source still adds. Higher = reaches high confidence with fewer sources."},
    {"key": "decay.ordinary", "label": "Corroboration value · ordinary claim", "group": "Extremity (§5.6)",
     "type": "float", "default": str(_DECAY["ordinary"]), "core": True, "source": "corroborate.py:_DECAY",
     "help": "How much each extra source adds for a slightly-above-routine claim."},
    {"key": "decay.notable", "label": "Corroboration value · notable claim", "group": "Extremity (§5.6)",
     "type": "float", "default": str(_DECAY["notable"]), "core": True, "source": "corroborate.py:_DECAY",
     "help": "How much each extra source adds for a notable claim."},
    {"key": "decay.significant", "label": "Corroboration value · significant claim", "group": "Extremity (§5.6)",
     "type": "float", "default": str(_DECAY["significant"]), "core": True, "source": "corroborate.py:_DECAY",
     "help": "How much each extra source adds for a significant claim."},
    {"key": "decay.extraordinary", "label": "Corroboration value · extraordinary claim",
     "group": "Extremity (§5.6)", "type": "float", "default": str(_DECAY["extraordinary"]), "core": True,
     "source": "corroborate.py:_DECAY",
     "help": "For an extraordinary claim: how much each extra source adds. Lower = it takes many independent sources to reach high confidence."},
    {"key": "weight.named", "label": "Trust · named source", "group": "Attribution (§5.2)",
     "type": "float", "default": str(_W_NAMED), "core": True, "source": "corroborate.py:_W_NAMED",
     "help": "How much a claim counts when it is attributed to a named source (the highest)."},
    {"key": "weight.anonymous", "label": "Trust · anonymous source", "group": "Attribution (§5.2)",
     "type": "float", "default": str(_W_ANONYMOUS), "core": True, "source": "corroborate.py:_W_ANONYMOUS",
     "help": "How much a claim counts when the source is anonymous (less than a named source)."},
    {"key": "weight.bald", "label": "Trust · no attribution", "group": "Attribution (§5.2)",
     "type": "float", "default": str(_W_BALD), "core": True, "source": "corroborate.py:_W_BALD",
     "help": "How much a claim counts when there is no attribution at all (the least)."},
    {"key": "confidence.primary_lift", "label": "Primary-source bonus", "group": "Confidence (§5.7)",
     "type": "float", "default": str(_PRIMARY_LIFT), "core": True, "source": "corroborate.py:_PRIMARY_LIFT",
     "help": "Reaching the original/primary source closes this fraction of the remaining gap to certainty."},
    {"key": "confidence.cap", "label": "Maximum confidence", "group": "Confidence (§5.7)",
     "type": "float", "default": str(_CONFIDENCE_CAP), "core": True, "source": "corroborate.py:_CONFIDENCE_CAP",
     "help": "The most confidence Maat will ever show — it never claims 100% certainty."},
    {"key": "cluster.same_fact", "label": "Same-story similarity", "group": "Clustering (§5.4-5.5)",
     "type": "float", "default": "0.82", "core": True, "source": "corroborate.py:same_fact_threshold",
     "help": "How similar two articles must be to count as the SAME story. Higher = stricter (fewer merges)."},
    {"key": "cluster.duplicate_source", "label": "Duplicate-source similarity",
     "group": "Clustering (§5.4-5.5)", "type": "float", "default": "0.40", "core": True,
     "source": "corroborate.py:duplicate_source_threshold",
     "help": "How similar two sources must be to be treated as one originator (e.g. wire copies), so echoing isn't mistaken for corroboration."},
    {"key": "cluster.min_corroboration", "label": "Min independent sources",
     "group": "Clustering (§5.4-5.5)", "type": "int", "default": "2", "core": True,
     "source": "corroborate.py:min_corroboration",
     "help": "How many INDEPENDENT originators a story needs before it counts as corroborated."},
]

KNOBS_BY_KEY: dict[str, dict] = {k["key"]: k for k in KNOBS}


def groups() -> list[str]:
    """Knob groups, in declaration order (for a stable, grouped render)."""
    out: list[str] = []
    for k in KNOBS:
        if k["group"] not in out:
            out.append(k["group"])
    return out


# --- Enactment (#183/#184) ----------------------------------------------------------------
# The knobs that map to corroborate()/confidence_read() parameters TODAY. The rest — model
# routing, the attribution weights (weight.*), and the confidence_label tier cut-points
# (gate.floor / tier.*) — aren't parameterised in the pipeline yet; promoting those is a follow-up.
_ENACTABLE = frozenset(
    {
        "decay.routine", "decay.ordinary", "decay.notable", "decay.significant", "decay.extraordinary",
        "confidence.primary_lift", "confidence.cap",
        "cluster.same_fact", "cluster.duplicate_source", "cluster.min_corroboration",
    }
)


def active_config(promoted_events) -> dict[str, float]:
    """Fold `admin.config.promoted` data dicts (oldest → newest) into {key: value} for the
    enactable knobs — the live, sign-off-gated overrides the pipeline reads instead of hardcoded
    constants. Latest promote per key wins; only parseable numerics for known enactable keys kept.
    """
    out: dict[str, float] = {}
    for e in promoted_events:
        d = json.loads(e) if isinstance(e, str) else e
        key, value = d.get("key"), d.get("value")
        if key in _ENACTABLE and value is not None:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
    return out


def pipeline_overrides(cfg: dict[str, float]) -> dict:
    """Map a flat active-config dict to the kwargs corroborate() takes (a decay dict + scalars),
    each falling back to its registered code default."""
    def g(key: str) -> float:
        return cfg.get(key, float(KNOBS_BY_KEY[key]["default"]))

    extremities = ("routine", "ordinary", "notable", "significant", "extraordinary")
    return {
        "decay": {ex: g(f"decay.{ex}") for ex in extremities},
        "primary_lift": g("confidence.primary_lift"),
        "cap": g("confidence.cap"),
        "same_fact_threshold": g("cluster.same_fact"),
        "duplicate_source_threshold": g("cluster.duplicate_source"),
        "min_corroboration": int(g("cluster.min_corroboration")),
    }
