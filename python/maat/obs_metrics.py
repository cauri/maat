"""Pipeline-health summaries for the operator console and alerting (issue #61, P7 §8).

Pure functions only — no database access, no I/O. All inputs are plain dicts / lists of dicts
that the caller has already fetched. This keeps every function unit-testable without a live DB.

Typical call site (async web handler, after pool.fetch calls)::

    from maat.obs_metrics import pipeline_health

    event_rows   = [dict(r) for r in await pool.fetch("select type, created_at from events")]
    dead_rows    = [dict(r) for r in await pool.fetch(
                       "select type, error, created_at from dead_letters order by id desc")]
    projection_counts = {
        "articles": await pool.fetchval("select count(*) from articles"),
        "claims":   await pool.fetchval("select count(*) from claims"),
        "clusters": await pool.fetchval("select count(*) from clusters"),
    }
    summary = pipeline_health(event_rows, dead_rows, projection_counts)

All ``datetime`` values in inputs are assumed to be timezone-aware (Postgres returns
``timestamptz``). The ``as_of`` parameter (default: ``datetime.now(UTC)``) lets tests
inject a frozen clock.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Stage definitions — must match the ``_STAGES`` list in web/app.py.
# ---------------------------------------------------------------------------

#: Logical stage name -> event type emitted when that stage completes one unit of work.
STAGE_EVENT_TYPES: dict[str, str] = {
    "acquire": "article.ingested",
    "extract": "claims.extracted",
    "classify": "claims.classified",
    "cluster": "cluster.corroborated",
}

#: Age thresholds (seconds) for freshness status labels.
STALE_THRESHOLD_S: int = 3600    # 1 hour  → "stale"
STALLED_THRESHOLD_S: int = 86400  # 24 hours → "stalled"


# ---------------------------------------------------------------------------
# Per-stage health
# ---------------------------------------------------------------------------


def stage_health(
    event_rows: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Summarise per-stage event counts and last-seen timestamps.

    Args:
        event_rows: Each dict must have at least ``{"type": str, "created_at": datetime}``.
        as_of: Reference time for age calculations. Defaults to ``datetime.now(UTC)``.

    Returns:
        One summary dict per stage in pipeline order::

            {
                "stage":      str,            # logical name (acquire/extract/classify/cluster)
                "event_type": str,            # raw event type matched
                "count":      int,            # total events of this type in the log
                "last_seen":  datetime | None,
                "freshness":  str,            # "fresh" / "stale" / "stalled" / "never"
                "age_s":      float | None,   # seconds since last event, or None
            }
    """
    now = as_of or datetime.now(timezone.utc)

    # Aggregate: event_type -> [count, latest_datetime]
    agg: dict[str, list] = {}
    for row in event_rows:
        etype = row.get("type") or ""
        ts = row.get("created_at")
        if etype not in agg:
            agg[etype] = [0, None]
        agg[etype][0] += 1
        if ts is not None:
            if agg[etype][1] is None or ts > agg[etype][1]:
                agg[etype][1] = ts

    out: list[dict[str, Any]] = []
    for stage, etype in STAGE_EVENT_TYPES.items():
        bucket = agg.get(etype, [0, None])
        count, last_seen = bucket[0], bucket[1]
        age_s, freshness = _freshness(last_seen, now)
        out.append(
            {
                "stage": stage,
                "event_type": etype,
                "count": count,
                "last_seen": last_seen,
                "freshness": freshness,
                "age_s": age_s,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Dead-letter summary
# ---------------------------------------------------------------------------


def dead_letter_summary(
    dead_rows: list[dict[str, Any]],
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Summarise dead-letter entries (failed-and-skipped items).

    Args:
        dead_rows: Each dict should have ``{"type": str, "error": str, "created_at": datetime}``.
            Pass pre-sorted newest-first (the typical DB query order).
        limit: Maximum number of recent entries to include verbatim in the output.

    Returns:
        ::

            {
                "total":         int,
                "recent":        list[dict],   # up to ``limit`` most-recent entries
                "error_preview": str,          # first error from the most-recent entry, truncated
            }
    """
    total = len(dead_rows)
    recent = dead_rows[:limit]
    error_preview = ""
    if recent:
        raw = recent[0].get("error") or ""
        error_preview = raw[:200] + ("…" if len(raw) > 200 else "")
    return {
        "total": total,
        "recent": recent,
        "error_preview": error_preview,
    }


# ---------------------------------------------------------------------------
# Projection sizes
# ---------------------------------------------------------------------------


def projection_sizes(counts: dict[str, int]) -> dict[str, int]:
    """Normalise projection row-counts into a standard shape.

    Args:
        counts: ``{"articles": int, "claims": int, "clusters": int}`` (extra keys ignored).

    Returns:
        Dict with zero-defaults for missing keys.
    """
    return {
        "articles": int(counts.get("articles") or 0),
        "claims": int(counts.get("claims") or 0),
        "clusters": int(counts.get("clusters") or 0),
    }


# ---------------------------------------------------------------------------
# Throughput / freshness read
# ---------------------------------------------------------------------------


def throughput_freshness(
    event_rows: list[dict[str, Any]],
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Report the age of the newest event in the log overall.

    A continuously running pipeline should always have a very recent event.
    A large ``newest_event_age_s`` indicates the pipeline has stalled or been paused.

    Args:
        event_rows: Each dict must have at least ``{"created_at": datetime}``.
        as_of: Reference time. Defaults to ``datetime.now(UTC)``.

    Returns:
        ::

            {
                "newest_event_age_s": float | None,
                "newest_event_at":    datetime | None,
                "freshness":          str,           # "fresh" / "stale" / "stalled" / "never"
            }
    """
    now = as_of or datetime.now(timezone.utc)
    newest: datetime | None = None
    for row in event_rows:
        ts = row.get("created_at")
        if ts is not None and (newest is None or ts > newest):
            newest = ts
    age_s, freshness = _freshness(newest, now)
    return {
        "newest_event_age_s": age_s,
        "newest_event_at": newest,
        "freshness": freshness,
    }


# ---------------------------------------------------------------------------
# Calibration / confidence health rollup
# ---------------------------------------------------------------------------


def calibration_health(
    clusters: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute a lightweight confidence-calibration health read from cluster projection rows.

    Pure — no DB access. Takes the ``clusters`` rows already fetched.
    Useful for a quick operator-console summary without running the full calibration harness.

    Args:
        clusters: Each dict should have
            ``{"confidence": float, "independent_originators": int,
               "has_primary": bool, "extremity": str}``.

    Returns:
        ::

            {
                "n":                     int,
                "well_corroborated":     int,   # confidence >= 0.7
                "thinly_sourced":        int,   # confidence < 0.5
                "single_source":         int,   # independent_originators == 1
                "has_primary_count":     int,
                "mean_confidence":       float | None,
                "confidence_distribution": {"hi": int, "mid": int, "lo": int, "floor": int},
            }
    """
    n = len(clusters)
    if n == 0:
        return {
            "n": 0,
            "well_corroborated": 0,
            "thinly_sourced": 0,
            "single_source": 0,
            "has_primary_count": 0,
            "mean_confidence": None,
            "confidence_distribution": {"hi": 0, "mid": 0, "lo": 0, "floor": 0},
        }

    distribution: dict[str, int] = {"hi": 0, "mid": 0, "lo": 0, "floor": 0}
    total_conf = 0.0
    well_corroborated = 0
    thinly_sourced = 0
    single_source = 0
    has_primary_count = 0

    for cl in clusters:
        conf = float(cl.get("confidence") or 0.0)
        total_conf += conf
        # Confidence distribution bands (mirrors confidence_label cut-points in corroborate.py)
        if conf >= 0.85:
            distribution["hi"] += 1
        elif conf >= 0.7:
            distribution["mid"] += 1
        elif conf >= 0.5:
            distribution["lo"] += 1
        else:
            distribution["floor"] += 1
        if conf >= 0.7:
            well_corroborated += 1
        if conf < 0.5:
            thinly_sourced += 1
        if int(cl.get("independent_originators") or 0) == 1:
            single_source += 1
        if cl.get("has_primary"):
            has_primary_count += 1

    return {
        "n": n,
        "well_corroborated": well_corroborated,
        "thinly_sourced": thinly_sourced,
        "single_source": single_source,
        "has_primary_count": has_primary_count,
        "mean_confidence": round(total_conf / n, 4),
        "confidence_distribution": distribution,
    }


# ---------------------------------------------------------------------------
# Top-level health summary
# ---------------------------------------------------------------------------


def pipeline_health(
    event_rows: list[dict[str, Any]],
    dead_rows: list[dict[str, Any]],
    projection_counts: dict[str, int],
    *,
    clusters: list[dict[str, Any]] | None = None,
    as_of: datetime | None = None,
    dead_letter_limit: int = 10,
) -> dict[str, Any]:
    """Produce a full pipeline-health summary for the operator console and alerting.

    Args:
        event_rows: All (or recent) event rows: ``{"type": str, "created_at": datetime}``.
        dead_rows: Dead-letter rows (newest-first):
            ``{"type": str, "error": str, "created_at": datetime}``.
        projection_counts: ``{"articles": int, "claims": int, "clusters": int}``.
        clusters: Optional cluster projection rows for ``calibration_health``.
        as_of: Reference time (inject in tests for a frozen clock).
        dead_letter_limit: Max recent dead-letter entries to embed.

    Returns:
        ::

            {
                "as_of":        str,              # ISO-8601
                "status":       str,              # "healthy" / "degraded" / "stalled" / "empty"
                "stages":       list[dict],       # one per stage
                "dead_letters": dict,
                "projections":  dict,
                "throughput":   dict,
                "calibration":  dict,
                "alerts":       list[str],        # human-readable; forward to alerting sinks
            }
    """
    now = as_of or datetime.now(timezone.utc)

    stages = stage_health(event_rows, as_of=now)
    dead = dead_letter_summary(dead_rows, limit=dead_letter_limit)
    proj = projection_sizes(projection_counts)
    throughput = throughput_freshness(event_rows, as_of=now)
    calib = calibration_health(clusters or [])

    alerts = _build_alerts(stages, dead, proj, throughput)
    status = _overall_status(stages, dead, proj)

    return {
        "as_of": now.isoformat(),
        "status": status,
        "stages": stages,
        "dead_letters": dead,
        "projections": proj,
        "throughput": throughput,
        "calibration": calib,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _freshness(ts: datetime | None, now: datetime) -> tuple[float | None, str]:
    """Return (age_seconds, freshness_label) for a timestamp relative to *now*."""
    if ts is None:
        return None, "never"
    # Defensively handle naive datetimes (tests may pass naive; Postgres always returns tz-aware).
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_s = max(0.0, (now - ts).total_seconds())
    if age_s < STALE_THRESHOLD_S:
        return age_s, "fresh"
    if age_s < STALLED_THRESHOLD_S:
        return age_s, "stale"
    return age_s, "stalled"


def _build_alerts(
    stages: list[dict[str, Any]],
    dead: dict[str, Any],
    proj: dict[str, int],
    throughput: dict[str, Any],
) -> list[str]:
    alerts: list[str] = []

    if dead["total"] > 0:
        preview = f" ({dead['error_preview'][:80]})" if dead["error_preview"] else ""
        alerts.append(
            f"{dead['total']} dead-letter item(s) — pipeline errors need review{preview}"
        )

    for s in stages:
        if s["freshness"] == "never":
            alerts.append(f"Stage '{s['stage']}' has never run")
        elif s["freshness"] in ("stale", "stalled") and s["count"] > 0:
            age_h = round((s["age_s"] or 0) / 3600, 1)
            alerts.append(f"Stage '{s['stage']}' last ran {age_h}h ago — consider re-running")

    if proj["articles"] == 0:
        alerts.append(
            "No articles ingested yet — run 'make acquire' or 'make ingest-corpus'"
        )
    elif proj["clusters"] == 0:
        alerts.append("Articles present but no clusters — run the full pipeline")

    if throughput["freshness"] == "stalled":
        age_h = round((throughput["newest_event_age_s"] or 0) / 3600, 1)
        alerts.append(
            f"No pipeline activity for {age_h}h — pipeline may be paused or broken"
        )

    return alerts


def _overall_status(
    stages: list[dict[str, Any]],
    dead: dict[str, Any],
    proj: dict[str, int],
) -> str:
    if proj["articles"] == 0 and proj["clusters"] == 0:
        return "empty"
    if dead["total"] > 0:
        return "degraded"
    stalled = [s for s in stages if s["freshness"] == "stalled" and s["count"] > 0]
    if stalled:
        return "stalled"
    return "healthy"
