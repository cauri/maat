"""Accuracy-axis lifecycle: dormant → resolving → resolved → extended / decayed (P3, §5).

A fact's accuracy STATE evolves over time as evidence accrues.  This module models that
lifecycle as pure functions over a fact's ordered corroboration trajectory — the sequence of
`cluster.corroborated` events (each a dict) for that fact, plus a reference time and a
staleness window.

States
------
dormant      — just seen, little independent corroboration yet (initial state)
resolving    — gaining independent corroboration across ticks (evidence is accruing)
resolved     — reached a terminal outcome (confirmed or refuted); see `resolve_outcome`
extended     — a resolved fact that keeps accruing evidence (still live, still of interest)
decayed      — went stale: no new corroboration within `stale_after` and never resolved

"Computed, not displayed." — these states drive internal routing and calibration; they are
not a reader-facing label (that is `confidence_label` / the gate label).

Pure functions over plain values — no DB, no I/O, deterministic (wall-clock never touches
the logic; pass `now` and timestamps in as arguments).
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum

from maat.learning.calibration import resolve_outcome


class LifecycleState(str, Enum):
    """The five accuracy-axis states a fact can occupy.

    String subclass so values serialise cleanly in JSON / event payloads without extra work.
    """

    DORMANT = "dormant"
    RESOLVING = "resolving"
    RESOLVED = "resolved"
    EXTENDED = "extended"
    DECAYED = "decayed"


# Default staleness window: if a fact goes this many seconds without new corroboration and has
# never resolved, it is considered decayed.  Callers override via `stale_after`.
_DEFAULT_STALE_AFTER: float = 86_400.0  # 24 hours


def _ts(event: Mapping) -> float:
    """Extract the event timestamp as a UTC epoch float.

    Accepts a pre-computed numeric epoch (`ts`, `timestamp`, or `epoch` key) or an ISO-8601
    string under `ts` / `timestamp`.  Returns 0.0 if nothing found — safe fallback that will
    never accidentally mark events as newer than `now`.
    """
    for key in ("ts", "timestamp", "epoch"):
        raw = event.get(key)
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return dt.timestamp()
            except ValueError:
                continue
    return 0.0


def _is_resolved_outcome(outcome: str) -> bool:
    """A 'confirmed' or 'refuted' outcome is terminal — the fact has resolved."""
    return outcome in ("confirmed", "refuted")


def classify_lifecycle(
    trajectory: list[Mapping],
    now: float | datetime,
    *,
    stale_after: float = _DEFAULT_STALE_AFTER,
    confirm_at: int = 3,
) -> LifecycleState:
    """Classify the current accuracy-axis state of ONE fact from its corroboration trajectory.

    Parameters
    ----------
    trajectory:
        Ordered list of `cluster.corroborated` events for a single fact (oldest → newest).
        Each event is a dict with at least `independent_originators` (int), `has_primary`
        (bool), and optionally `corrected` (bool) and a timestamp key (`ts`/`timestamp`).
    now:
        Reference time — a UTC epoch float or a `datetime` (tz-aware or naive-UTC).  Passed
        in explicitly so the function is deterministic (no `datetime.now()` inside).
    stale_after:
        Seconds of silence that mark a never-resolved fact as decayed.
    confirm_at:
        Independent-originator threshold to reach `confirmed`; forwarded to
        `resolve_outcome` so the bar is consistent with the calibration layer.

    Returns
    -------
    LifecycleState
    """
    if not trajectory:
        return LifecycleState.DORMANT

    if isinstance(now, datetime):
        now_epoch = now.timestamp()
    else:
        now_epoch = float(now)

    first = trajectory[0]
    last = trajectory[-1]

    initial_ind = int(first.get("independent_originators", 0))
    latest_ind = int(last.get("independent_originators", 0))
    latest_has_primary = bool(last.get("has_primary", False))
    corrected = any(bool(e.get("corrected")) for e in trajectory)

    outcome = resolve_outcome(
        initial_ind, latest_ind,
        latest_has_primary=latest_has_primary,
        corrected=corrected,
        grounding=last.get("grounding"),
        confirm_at=confirm_at,
    )

    if _is_resolved_outcome(outcome):
        # If more events arrived AFTER the point resolution was first reachable, it is extended.
        # We check by comparing trajectory length: if there's more than one event and the latest
        # differs from the first, evidence kept accruing after resolution.
        if len(trajectory) > 1 and (latest_ind > initial_ind or latest_has_primary != bool(first.get("has_primary", False))):
            return LifecycleState.EXTENDED
        return LifecycleState.RESOLVED

    # Not yet resolved — check for staleness.
    last_ts = _ts(last)
    if last_ts > 0 and (now_epoch - last_ts) > stale_after:
        return LifecycleState.DECAYED

    # Still live: dormant (no growth) or resolving (evidence accruing).
    if latest_ind > initial_ind:
        return LifecycleState.RESOLVING

    return LifecycleState.DORMANT


def _norm_fact(fact: str) -> str:
    return " ".join((fact or "").lower().split())


def lifecycle_by_fact(
    events: Iterable[Mapping],
    now: float | datetime,
    *,
    stale_after: float = _DEFAULT_STALE_AFTER,
    confirm_at: int = 3,
) -> dict[str, LifecycleState]:
    """Fold a stream of `cluster.corroborated` events into a per-fact lifecycle state.

    Groups events by normalised fact text (same case/whitespace-collapsing as
    `observations_from_history`), then calls `classify_lifecycle` for each group.

    Parameters
    ----------
    events:
        Iterable of `cluster.corroborated` event dicts, oldest → newest.  Each must have a
        `fact` key; other keys are the standard corroboration fields.
    now:
        Reference time — epoch float or tz-aware datetime.
    stale_after:
        Staleness window in seconds (forwarded to `classify_lifecycle`).
    confirm_at:
        Corroboration bar (forwarded to `classify_lifecycle` / `resolve_outcome`).

    Returns
    -------
    dict mapping normalised fact text → LifecycleState
    """
    by_fact: OrderedDict[str, list[Mapping]] = OrderedDict()
    for e in events:
        key = _norm_fact(e.get("fact", ""))
        by_fact.setdefault(key, []).append(e)

    return {
        fact: classify_lifecycle(trajectory, now, stale_after=stale_after, confirm_at=confirm_at)
        for fact, trajectory in by_fact.items()
    }
