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

from maat.pipeline.classify import CLASSIFY_MODEL
from maat.pipeline.corroborate import _DECAY
from maat.pipeline.extremity import EXTREMITY_MODEL
from maat.providers.seam import CLAUDE_JUDGE, MISTRAL_BULK, MISTRAL_EMBED

# Each knob: key, human label, group, current default (from live code), core?, code source.
# The threshold/clustering literals live inside functions (not importable constants), so they
# are mirrored here with their source — promotion would lift them to a single config read.
KNOBS: list[dict] = [
    {"key": "model.judge", "label": "Judge model (Claude)", "group": "Model routing",
     "default": CLAUDE_JUDGE, "core": True, "source": "providers/seam.py:CLAUDE_JUDGE"},
    {"key": "model.classify", "label": "Fact/projection classifier", "group": "Model routing",
     "default": CLASSIFY_MODEL, "core": True, "source": "pipeline/classify.py:CLASSIFY_MODEL"},
    {"key": "model.extremity", "label": "Extremity rater", "group": "Model routing",
     "default": EXTREMITY_MODEL, "core": False, "source": "pipeline/extremity.py:EXTREMITY_MODEL"},
    {"key": "model.bulk", "label": "Bulk model (Mistral)", "group": "Model routing",
     "default": MISTRAL_BULK, "core": False, "source": "providers/seam.py:MISTRAL_BULK"},
    {"key": "model.embed", "label": "Embedding model", "group": "Model routing",
     "default": MISTRAL_EMBED, "core": False, "source": "providers/seam.py:MISTRAL_EMBED"},
    {"key": "gate.floor", "label": "Gate floor — suppress below", "group": "Veracity thresholds (§5.7)",
     "default": "0.40", "core": True, "source": "corroborate.py:confidence_label"},
    {"key": "tier.corroborated", "label": "'Corroborated' at", "group": "Veracity thresholds (§5.7)",
     "default": "0.60", "core": True, "source": "corroborate.py:confidence_label"},
    {"key": "tier.well", "label": "'Well corroborated' at", "group": "Veracity thresholds (§5.7)",
     "default": "0.85", "core": True, "source": "corroborate.py:confidence_label"},
    {"key": "decay.ordinary", "label": "Per-originator doubt · ordinary", "group": "Extremity (§5.6)",
     "default": str(_DECAY["ordinary"]), "core": True, "source": "corroborate.py:_DECAY"},
    {"key": "decay.notable", "label": "Per-originator doubt · notable", "group": "Extremity (§5.6)",
     "default": str(_DECAY["notable"]), "core": True, "source": "corroborate.py:_DECAY"},
    {"key": "decay.extraordinary", "label": "Per-originator doubt · extraordinary",
     "group": "Extremity (§5.6)", "default": str(_DECAY["extraordinary"]), "core": True,
     "source": "corroborate.py:_DECAY"},
    {"key": "cluster.same_fact", "label": "Same-fact threshold", "group": "Clustering (§5.4-5.5)",
     "default": "0.82", "core": True, "source": "corroborate.py:same_fact_threshold"},
    {"key": "cluster.duplicate_source", "label": "Originator-collapse threshold",
     "group": "Clustering (§5.4-5.5)", "default": "0.40", "core": True,
     "source": "corroborate.py:duplicate_source_threshold"},
    {"key": "cluster.min_corroboration", "label": "Min independent originators",
     "group": "Clustering (§5.4-5.5)", "default": "2", "core": True,
     "source": "corroborate.py:min_corroboration"},
]

KNOBS_BY_KEY: dict[str, dict] = {k["key"]: k for k in KNOBS}


def groups() -> list[str]:
    """Knob groups, in declaration order (for a stable, grouped render)."""
    out: list[str] = []
    for k in KNOBS:
        if k["group"] not in out:
            out.append(k["group"])
    return out
