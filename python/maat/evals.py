"""Eval harness (§4, a P1 exit criterion) — measure the veracity pipeline.

Runs over the projections after a pipeline pass: reports per-stage metrics and a
golden-regression check on the known fixtures. It is the instrument that would have caught
the #20 over-merge automatically ("the resignation must collapse to 3 independent
originators, and must not absorb the gold story"). Run: `make eval`.

cat-cafe wiring (OTLP sink + immediate LLM judge) is deferred — flagged for cauri, since it
sits near the Gamelan IP boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from maat.pipeline.corroborate import confidence_label

ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS = ROOT / "eval" / "expectations.json"


def _aslist(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


@dataclass
class Check:
    field: str
    ok: bool
    detail: str


@dataclass
class StoryResult:
    name: str
    matched: bool
    fact: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.matched and all(c.ok for c in self.checks)


def _headline_for(clusters, match: str):
    """The most-asserted (most-sourced) cluster whose fact contains `match` — the headline."""
    hits = [c for c in clusters if match.lower() in (c["fact"] or "").lower()]
    return max(hits, key=lambda c: len(_aslist(c["sources"])), default=None)


def metrics(clusters, claims) -> dict:
    confs = [float(c["confidence"] or 0) for c in clusters]
    labels: dict[str, int] = {}
    for c in clusters:
        lbl = confidence_label(float(c["confidence"] or 0))[0]
        labels[lbl] = labels.get(lbl, 0) + 1
    kinds: dict[str, int] = {}
    for c in claims:
        k = c.get("kind") or "unclassified"
        kinds[k] = kinds.get(k, 0) + 1
    return {
        "claims": len(claims),
        "claim_kinds": kinds,
        "clusters": len(clusters),
        "with_primary": sum(1 for c in clusters if c["has_primary"]),
        "extraordinary": sum(1 for c in clusters if c["extremity"] == "extraordinary"),
        "confidence_mean": round(sum(confs) / len(confs), 2) if confs else 0.0,
        "labels": labels,
    }


def check_story(clusters, name: str, exp: dict) -> StoryResult:
    cl = _headline_for(clusters, exp["match"])
    if cl is None:
        return StoryResult(name=name, matched=False, fact="(no cluster matched)")
    res = StoryResult(name=name, matched=True, fact=cl["fact"])
    ind = cl["independent_originators"]
    if "independent_originators" in exp:
        want = exp["independent_originators"]
        res.checks.append(Check("independent_originators", ind == want, f"{ind} (want {want})"))
    if "max_independent_originators" in exp:
        m = exp["max_independent_originators"]
        res.checks.append(Check("independent_originators", ind <= m, f"{ind} (want <= {m})"))
    if "extremity" in exp:
        got = cl["extremity"]
        res.checks.append(Check("extremity", got == exp["extremity"], f"{got} (want {exp['extremity']})"))
    if "has_primary" in exp:
        got = bool(cl["has_primary"])
        res.checks.append(Check("has_primary", got == exp["has_primary"], f"{got} (want {exp['has_primary']})"))
    if "label" in exp:
        got = confidence_label(float(cl["confidence"] or 0))[0]
        res.checks.append(Check("label", got == exp["label"], f"{got!r} (want {exp['label']!r})"))
    return res


def evaluate(clusters, claims, expectations) -> dict:
    stories = [
        check_story(clusters, name, exp)
        for name, exp in expectations.items()
        if not name.startswith("_")
    ]
    return {
        "metrics": metrics(clusters, claims),
        "stories": stories,
        "passed": all(s.ok for s in stories),
    }


def load_expectations(path: Path = EXPECTATIONS) -> dict:
    return json.loads(path.read_text())
