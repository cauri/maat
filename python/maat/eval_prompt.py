"""Eval-on-change for prompt edits (P8 / PLAN §7).

Run the golden corpus through the pipeline IN MEMORY with a given set of prompts, then score it
with the eval harness — so an operator can see the pass/fail BEFORE relying on an edited prompt.
No DB writes: the pure pipeline functions return values, so the live projections are never
touched. The LLM calls are real (this costs money), so it is always a deliberate action.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

from maat.evals import evaluate, load_expectations
from maat.pipeline.classify import classify_claims
from maat.pipeline.corroborate import ClaimRow, corroborate
from maat.pipeline.extract import extract_claims
from maat.pipeline.extremity import rate_extremity

ROOT = Path(__file__).resolve().parents[2]


def load_corpus(root: Path = ROOT) -> list[dict]:
    """Every golden fixture article (corpus/*.json)."""
    arts: list[dict] = []
    for path in sorted((root / "corpus").glob("*.json")):
        arts.extend(json.loads(path.read_text()).get("articles", []))
    return arts


def run_goldens(articles, *, extract_prompt: str, classify_prompt: str, extremity_prompt: str):
    """Run the pipeline over `articles` in memory with the given prompts. Returns (clusters,
    claims) shaped for maat.evals.evaluate(). Makes live LLM calls; writes nothing to the store.
    """
    rows: list[ClaimRow] = []
    bodies: dict[str, str] = {}
    claims_meta: list[dict] = []
    for a in articles:
        aid = a["id"]
        bodies[aid] = a.get("body", "")
        claims = extract_claims(
            a.get("body", ""),
            source_metadata=a.get("source", ""),
            language=a.get("language", "en"),
            prompt=extract_prompt,
        )
        claims = classify_claims(claims, article_text=a.get("body", ""), prompt=classify_prompt)
        for c in claims:
            rows.append(ClaimRow(id=c.id, text=c.text, article_id=aid, source=a.get("source", "")))
            claims_meta.append({"kind": c.kind})
    corrs = corroborate(rows, bodies, extremity_of=partial(rate_extremity, prompt=extremity_prompt))
    clusters = [
        {
            "fact": r.fact,
            "sources": r.sources,
            "originators": r.originators,
            "independent_originators": r.independent_originators,
            "has_primary": r.has_primary,
            "confidence": r.confidence,
            "extremity": r.extremity,
        }
        for r in corrs
    ]
    return clusters, claims_meta


def eval_goldens(*, extract_prompt: str, classify_prompt: str, extremity_prompt: str,
                 expectations: dict | None = None) -> dict:
    """Run the goldens with the given prompts and score them — returns evaluate()'s report."""
    clusters, claims = run_goldens(
        load_corpus(),
        extract_prompt=extract_prompt,
        classify_prompt=classify_prompt,
        extremity_prompt=extremity_prompt,
    )
    exp = expectations if expectations is not None else load_expectations()
    return evaluate(clusters, claims, exp)


def summary(report: dict) -> str:
    """One-line pass/fail headline for a report, with the first failing check (P8). Pure."""
    n_ok = sum(1 for s in report["stories"] if s.ok)
    total = len(report["stories"])
    head = f"{'PASS' if report['passed'] else 'FAIL'} — {n_ok}/{total} golden stories"
    if report["passed"]:
        return head
    for s in report["stories"]:
        if s.ok:
            continue
        if not s.matched:
            return f"{head} · {s.name}: no matching story produced"
        bad = next((c for c in s.checks if not c.ok), None)
        return f"{head} · {s.name}: {bad.field} {bad.detail}" if bad else f"{head} · {s.name} failed"
    return head
