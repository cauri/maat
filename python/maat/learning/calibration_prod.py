"""Calibration-in-production (P7, Issue #60) — a live, observable form of the offline loop.

`production_calibration(events, *, now)` runs the full calibration pipeline over the live
`cluster.corroborated` history and returns a `CalibrationStatus` suitable for the P8 dashboard
and alerting: current Brier score, reliability bins, pending tune proposals, the refutation-bias
caveat, and freshness metadata.

Sign-off gate is fully preserved: nothing is auto-applied. `tune_proposals` returns suggestions
keyed to the Config registry; an operator still signs off via the Config panel before anything
changes.

Pure function over plain values — no DB, no I/O. `scripts/calibrate_prod.py` feeds it the
live event stream and handles output/scheduling.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


from maat.learning.calibration import (
    Bin,
    Observation,
    Weights,
    brier_score,
    calibration_bins,
    observations_from_history,
    tune_proposals,
)


# --- Status dataclass ---------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationStatus:
    """Snapshot of how well the current confidence weights are calibrated.

    Returned by `production_calibration`. Pure data — no DB, no I/O.

    Fields
    ------
    brier:              Current Brier score under the live weights (None = no facts resolved yet).
    bins:               Reliability table — per confidence band, mean read vs fraction confirmed.
    proposals:          Weight changes the tuner would suggest (Config registry format).  Empty
                        when there is no scored history.
    n_observations:     Total facts in the history (including still-in-flight).
    n_scored:           Facts that reached a terminal outcome (confirmed or refuted).
    refutation_bias:    True when every resolved fact confirmed (no refutations seen).  Proposals
                        skew optimistic; the P8 UI surfaces this caveat.
    as_of:              When this status was computed (UTC).
    freshness_seconds:  Age of the most recent event in the history, in seconds from `now`.
                        None when the history is empty.
    """

    brier: float | None
    bins: list[Bin]
    proposals: list[dict]
    n_observations: int
    n_scored: int
    refutation_bias: bool
    as_of: datetime.datetime
    freshness_seconds: float | None


# --- Pure builder -------------------------------------------------------------------------


def production_calibration(
    events: Iterable[Mapping],
    *,
    now: datetime.datetime | None = None,
) -> CalibrationStatus:
    """Run the full calibration pipeline over a `cluster.corroborated` event stream.

    Parameters
    ----------
    events:
        An iterable of ``cluster.corroborated`` event payloads, oldest first.  Each mapping
        must carry at minimum the fields that `observations_from_history` expects (``fact``,
        ``independent_originators``, ``has_primary``, ``extremity``, optionally ``corrected``
        and ``occurred_at`` / ``ts`` for freshness).
    now:
        The clock reference for freshness calculations.  Defaults to
        ``datetime.datetime.now(UTC)``.  Pass an explicit value in tests or when the
        scheduler supplies the timestamp.

    Returns
    -------
    CalibrationStatus
        A fully-populated snapshot ready for the P8 dashboard.  Never mutates anything; the
        sign-off gate in the Config panel is the only path to applying proposals.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    raw = list(events)
    obs: list[Observation] = observations_from_history(raw)
    base = Weights.defaults()

    # Core calibration metrics (pure functions from calibration.py)
    score = brier_score(obs, base)
    bins = calibration_bins(obs, base)

    # Outcome breakdown for the bias flag
    outcomes = {o.outcome for o in obs}
    n_scored = sum(1 for o in obs if o.outcome in ("confirmed", "refuted"))
    refutation_bias = (
        n_scored > 0
        and "confirmed" in outcomes
        and "refuted" not in outcomes
    )

    # Proposals when we have scored data; the refutation_bias flag lets the dashboard warn
    # that they skew optimistic but we still surface them so the operator can judge.
    proposals = tune_proposals(obs, base=base) if n_scored > 0 else []

    # Freshness: age of the most-recent event in the history
    freshness_seconds: float | None = None
    if raw:
        # events carry ``occurred_at`` (ISO string) or ``ts`` (epoch float / ISO); fall back
        # gracefully when neither is present.
        latest_ts: datetime.datetime | None = None
        for ev in raw:
            ts = _parse_ts(ev.get("occurred_at") or ev.get("ts"))
            if ts is not None and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
        if latest_ts is not None:
            # ensure both datetimes are offset-aware before subtracting
            if latest_ts.tzinfo is None:
                latest_ts = latest_ts.replace(tzinfo=datetime.timezone.utc)
            delta = now - latest_ts
            freshness_seconds = max(0.0, delta.total_seconds())

    return CalibrationStatus(
        brier=score,
        bins=bins,
        proposals=proposals,
        n_observations=len(obs),
        n_scored=n_scored,
        refutation_bias=refutation_bias,
        as_of=now,
        freshness_seconds=freshness_seconds,
    )


# --- Helpers ------------------------------------------------------------------------------


def _parse_ts(raw: object) -> datetime.datetime | None:
    """Coerce a timestamp value (ISO string or epoch float/int) to a datetime, or None."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(float(raw), tz=datetime.timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        # Try common ISO forms (with and without timezone suffix)
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.datetime.strptime(raw, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                return dt
            except ValueError:
                continue
        # fromisoformat handles Python 3.7+ ISO subset; widest net last
        try:
            dt = datetime.datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except ValueError:
            return None
    return None


# --- Formatting helper (used by the runner) -----------------------------------------------


def format_status(status: CalibrationStatus) -> str:
    """Human-readable summary of a CalibrationStatus (for CLI / log output)."""
    lines: list[str] = []
    lines.append(f"calibration snapshot  as_of={status.as_of.isoformat()}")
    lines.append(f"  observations: {status.n_observations}  scored: {status.n_scored}")

    if status.freshness_seconds is None:
        lines.append("  freshness: no events in history")
    else:
        age_h = status.freshness_seconds / 3600
        lines.append(f"  freshness: most-recent event {age_h:.1f}h ago")

    if status.brier is None:
        lines.append(
            "\n  Nothing has resolved yet — the loop activates as the clock keeps acquiring."
        )
        return "\n".join(lines)

    lines.append(f"\n  Brier score: {status.brier}  (lower = better-calibrated)")
    lines.append("  reliability bins (predicted read vs fraction confirmed):")
    for b in status.bins:
        flag = ""
        if b.predicted + 0.10 < b.actual:
            flag = "  <- under-confident"
        elif b.predicted > b.actual + 0.10:
            flag = "  <- over-confident"
        lines.append(
            f"    [{b.lo:.2f},{b.hi:.2f})  n={b.n}  "
            f"read={b.predicted}  confirmed={b.actual}{flag}"
        )

    if status.refutation_bias:
        lines.append(
            "\n  CAVEAT: every resolved fact confirmed — no refutations in view.  Until a"
            " refutation signal (retraction, contradicting fact) feeds the loop, proposals"
            " skew toward higher confidence and should be treated as provisional."
        )

    if status.proposals:
        lines.append(f"\n  tune proposals ({len(status.proposals)}, needs operator sign-off):")
        for p in status.proposals:
            lines.append(f"    {p['key']} -> {p['value']}  ({p['reason']})")
    else:
        lines.append(
            "\n  no tune proposals — current weights already fit the resolved history."
        )

    return "\n".join(lines)
