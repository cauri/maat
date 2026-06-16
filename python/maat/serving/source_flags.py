"""Operator source allow/deny enforcement (#187, integrity).

The operator flags a source allow/deny on /sources (`admin.source.flagged`). These folds turn the
event stream into the current allow/deny sets so acquisition can refuse to pull a denied source
and serving can drop stories sourced entirely from denied sources. Pure — the caller runs the
query (`select data from events where type='admin.source.flagged' order by id`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping


def fold_source_flags(events: Iterable[Mapping | str]) -> dict[str, str]:
    """Latest allow/deny per source from `admin.source.flagged` data dicts (oldest → newest)."""
    out: dict[str, str] = {}
    for e in events:
        d = json.loads(e) if isinstance(e, str) else e
        src = d.get("source")
        status = d.get("status")
        if src and status in ("allow", "deny"):
            out[src] = status
    return out


def denied_sources(events: Iterable[Mapping | str]) -> set[str]:
    """The set of currently-denied sources (latest flag per source wins)."""
    return {s for s, status in fold_source_flags(events).items() if status == "deny"}
