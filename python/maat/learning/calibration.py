"""Calibration + auto-tuning of the confidence weights (P3, §5.8 — the truth-over-time pillar).

cauri: the weights are starting points — "better is to build ways to assess their accuracy over
time and adjust them automatically." This is that loop, in offline/replay form:

  ASSESS  — `brier_score` / `calibration_bins` score the EARLY confidence reads against how each
            fact actually resolved. Lower Brier = better-calibrated reads.
  ADJUST  — `tune_decay` searches for the per-extremity decay constants that would have scored
            best. It returns a SUGGESTION; promoting it is gated on operator sign-off and an
            A/B-on-replay pass (D18) — the same guardrail the Config panel enforces. Never
            auto-applied.

The "truth over time" label needs no external ground truth: a fact's own later, stronger evidence
is the signal. As the ingestion clock keeps acquiring, a real fact accrues independent
corroboration (or its primary source surfaces); a thin rumour stalls, or draws a correction.
`resolve_outcome` reads that trajectory off a fact's corroboration history.

Pure functions over plain values — no DB, no I/O. `scripts/calibrate.py` feeds it the
`cluster.corroborated` event history; the eval fixtures seed it before real history accrues.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from maat.pipeline.corroborate import _CONFIDENCE_CAP, _DECAY, _PRIMARY_LIFT, confidence_read


@dataclass(frozen=True)
class Weights:
    """The tunable confidence weights, in one object — what the Config panel surfaces."""

    decay: Mapping[str, float]
    primary_lift: float
    cap: float

    @classmethod
    def defaults(cls) -> Weights:
        return cls(decay=dict(_DECAY), primary_lift=_PRIMARY_LIFT, cap=_CONFIDENCE_CAP)

    def read(self, independent_originators: int, has_primary: bool, extremity: str) -> float:
        """The confidence read under THESE weights (scores the live function, not a copy)."""
        return confidence_read(
            independent_originators, has_primary, extremity,
            decay=dict(self.decay), primary_lift=self.primary_lift, cap=self.cap,
        )


# Terminal outcomes are calibration targets; the rest are still in flight and not yet scorable.
CONFIRMED, REFUTED, UNCONFIRMED, CORROBORATING = "confirmed", "refuted", "unconfirmed", "corroborating"
_TARGET = {CONFIRMED: 1.0, REFUTED: 0.0}


def resolve_outcome(
    initial_independent: int, latest_independent: int, *,
    latest_has_primary: bool, corrected: bool, confirm_at: int = 3,
) -> str:
    """Label a fact by how its corroboration evolved — the truth-over-time signal.

    confirmed    — it reached independent corroboration (or its primary source surfaced);
    refuted      — a correction / retraction attached to it;
    corroborating — it gained ground but hasn't cleared the bar (not yet terminal);
    unconfirmed  — it never grew past its initial thin state.
    """
    if corrected:
        return REFUTED
    if latest_independent >= confirm_at or latest_has_primary:
        return CONFIRMED
    if latest_independent > initial_independent:
        return CORROBORATING
    return UNCONFIRMED


@dataclass(frozen=True)
class Observation:
    """A fact whose INITIAL read we score against how it later resolved."""

    independent_originators: int  # at the initial read
    has_primary: bool             # at the initial read
    extremity: str
    outcome: str                  # from resolve_outcome


def _scorable(observations: Iterable[Observation]) -> list[Observation]:
    """Only facts that reached a terminal outcome can be scored against a 0/1 target."""
    return [o for o in observations if o.outcome in _TARGET]


def brier_score(observations: Iterable[Observation], weights: Weights | None = None) -> float | None:
    """Mean squared error of the initial reads vs terminal outcomes (lower = better-calibrated).

    None when nothing has resolved yet — the honest answer before history accrues.
    """
    w = weights or Weights.defaults()
    scored = _scorable(observations)
    if not scored:
        return None
    total = sum(
        (w.read(o.independent_originators, o.has_primary, o.extremity) - _TARGET[o.outcome]) ** 2
        for o in scored
    )
    return round(total / len(scored), 4)


@dataclass(frozen=True)
class Bin:
    lo: float
    hi: float
    n: int
    predicted: float  # mean confidence read in this band
    actual: float     # fraction that actually confirmed


def calibration_bins(
    observations: Iterable[Observation], weights: Weights | None = None, *,
    edges: tuple[float, ...] = (0.0, 0.4, 0.6, 0.85, 1.01),
) -> list[Bin]:
    """Reliability table: per confidence band, the mean read vs the fraction that confirmed.

    Well-calibrated reads sit near the diagonal (predicted ≈ actual). Systematic gaps say which
    way to move the weights — reads of 0.5 that confirm 80% of the time are under-confident.
    """
    w = weights or Weights.defaults()
    scored = _scorable(observations)
    reads = {id(o): w.read(o.independent_originators, o.has_primary, o.extremity) for o in scored}
    out: list[Bin] = []
    for lo, hi in zip(edges, edges[1:]):
        band = [o for o in scored if lo <= reads[id(o)] < hi]
        if not band:
            continue
        preds = [reads[id(o)] for o in band]
        confirmed = sum(1 for o in band if o.outcome == CONFIRMED)
        out.append(Bin(lo, hi, len(band), round(sum(preds) / len(preds), 3), round(confirmed / len(band), 3)))
    return out


# A coarse grid over the per-originator doubt; the search is offline so this is cheap.
_DECAY_GRID = (0.30, 0.40, 0.50, 0.55, 0.60, 0.66, 0.70, 0.76, 0.82)


def tune_decay(
    observations: Iterable[Observation], *,
    base: Weights | None = None, grid: tuple[float, ...] = _DECAY_GRID,
) -> tuple[Weights, float | None]:
    """Search the per-extremity decay constants that would best-calibrate the past reads.

    A SUGGESTION, never auto-applied (operator sign-off + the A/B-on-replay gate, D18). Each
    extremity's decay only touches facts at that level, so the levels are searched independently.
    Returns the best weights found and their Brier; the base weights unchanged if nothing scored.
    """
    base = base or Weights.defaults()
    scored = _scorable(observations)
    if not scored:
        return base, None
    tuned_decay = dict(base.decay)
    for level in base.decay:
        facts = [o for o in scored if o.extremity == level]
        if not facts:
            continue  # no evidence at this level — leave the starting point untouched
        best_v, best_b = base.decay[level], None
        for v in grid:
            candidate = replace(base, decay={**tuned_decay, level: v})
            b = brier_score(facts, candidate)
            if b is not None and (best_b is None or b < best_b):
                best_v, best_b = v, b
        tuned_decay[level] = best_v
    tuned = replace(base, decay=tuned_decay)
    return tuned, brier_score(scored, tuned)


def tune_proposals(observations: Iterable[Observation], *, base: Weights | None = None) -> list[dict]:
    """The decay changes the tuner would propose, as ``{key, value, reason}`` records.

    Keyed to the Config registry (``decay.<level>``), so they file directly as
    ``admin.threshold.changed`` proposals and surface on the matching knob in the Config panel.
    The operator signs off (or not) — these are never auto-applied.
    """
    base = base or Weights.defaults()
    obs = list(observations)
    tuned, tuned_b = tune_decay(obs, base=base)
    base_b = brier_score(obs, base)
    n = len(_scorable(obs))
    out: list[dict] = []
    for level, after in tuned.decay.items():
        before = base.decay[level]
        if before != after:
            out.append({
                "key": f"decay.{level}",
                "value": str(after),
                "reason": f"auto-tune: {before}→{after} (Brier {base_b}→{tuned_b}, n={n} resolved facts)",
            })
    return out


def _norm_fact(fact: str) -> str:
    return " ".join((fact or "").lower().split())


def observations_from_history(events: Iterable[Mapping]) -> list[Observation]:
    """Turn a `cluster.corroborated` event stream (oldest→newest) into resolved observations.

    Group by fact: the first event is the initial read, the last is where the fact stands now,
    and the trajectory between them gives the truth-over-time outcome. The label needs no external
    ground truth — the fact's own later, stronger evidence is the signal.
    """
    by_fact: OrderedDict[str, list[Mapping]] = OrderedDict()
    for e in events:
        by_fact.setdefault(_norm_fact(e.get("fact", "")), []).append(e)
    out: list[Observation] = []
    for hist in by_fact.values():
        first, last = hist[0], hist[-1]
        outcome = resolve_outcome(
            int(first.get("independent_originators", 0)),
            int(last.get("independent_originators", 0)),
            latest_has_primary=bool(last.get("has_primary", False)),
            corrected=any(h.get("corrected") for h in hist),
        )
        out.append(
            Observation(
                int(first.get("independent_originators", 0)),
                bool(first.get("has_primary", False)),
                first.get("extremity", "notable"),
                outcome,
            )
        )
    return out
