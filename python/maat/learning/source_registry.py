"""Source registry + lifecycle (#241) — a pure fold over ``source.*`` events.

cauri's pattern: a newly-seen acquisition source shouldn't hit the live feed until its articles
have flowed through the pipeline and earned a reputation — "ok if it doesn't appear until it is
complete." So every source carries a lifecycle:

    registered → backfilling → scored → active

and the live feed shows only sources that are NOT explicitly pending. The gate is **fail-open**:
a source the registry has never heard of is shown, so the registry can never silently empty the
feed (the same safety posture as the operator deny-list, which only ever *removes*).

Event-sourced exactly like reputation (#37): there is no kernel table. The registry agent
(``scripts/source_registry_agent.py``) emits ``source.registered`` / ``source.state_changed``;
this module folds them at read time. A source is activated once its articles have been
corroborated into clusters (it is genuinely in the feed) — which also grandfathers every source
already serving the live feed straight to ``active`` on the first run, so nothing that is showing
today is hidden.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Lifecycle states ----------------------------------------------------------------------------
REGISTERED = "registered"   # newly seen; articles not yet corroborated / scored
BACKFILLING = "backfilling"  # a backfill run is actively pulling its history (#241 part 2)
SCORED = "scored"           # backfill done + reputation computed, awaiting activation
ACTIVE = "active"           # in the live feed

# Everything short of ACTIVE is held out of the feed.
PENDING_STATES = frozenset({REGISTERED, BACKFILLING, SCORED})


@dataclass
class SourceRecord:
    source: str
    state: str = REGISTERED
    provider: str = ""
    reputation: float | None = None
    first_registered_at: str = ""
    last_changed_at: str = ""
    backfill_run_id: str = ""
    cost_usd: float = 0.0


def fold_sources(payloads: Iterable[Mapping]) -> dict[str, SourceRecord]:
    """Fold ``source.registered`` + ``source.state_changed`` data payloads (oldest→newest) into the
    current registry. Both event types carry the same shape, so the fold is type-agnostic:
    last-write-wins per field, with the first sighting fixing ``first_registered_at``."""
    out: dict[str, SourceRecord] = {}
    for d in payloads:
        src = (d.get("source") or "").strip()
        if not src:
            continue
        rec = out.get(src)
        if rec is None:
            rec = SourceRecord(source=src)
            out[src] = rec
        if d.get("state"):
            rec.state = d["state"]
        if d.get("provider"):
            rec.provider = d["provider"]
        if d.get("reputation") is not None:
            rec.reputation = float(d["reputation"])
        if d.get("run_id"):
            rec.backfill_run_id = d["run_id"]
        if d.get("cost_usd") is not None:
            rec.cost_usd = float(d["cost_usd"])
        at = d.get("at") or ""
        if at and not rec.first_registered_at:
            rec.first_registered_at = at
        if at:
            rec.last_changed_at = at
    return out


def pending_sources(records: Mapping[str, SourceRecord]) -> set[str]:
    """Sources explicitly held out of the live feed (registered / backfilling / scored)."""
    return {s for s, r in records.items() if r.state in PENDING_STATES}


def active_sources(records: Mapping[str, SourceRecord]) -> set[str]:
    return {s for s, r in records.items() if r.state == ACTIVE}


@dataclass(frozen=True)
class Transition:
    """A state change the agent should emit (as source.registered for new, else source.state_changed)."""
    source: str
    state: str
    reputation: float | None
    provider: str
    is_new: bool


def plan_registry(
    *,
    records: Mapping[str, SourceRecord],
    sources_seen: Iterable[str],
    provider_by_source: Mapping[str, str],
    sources_with_clusters: Iterable[str],
    reputation_by_source: Mapping[str, float],
    rep_epsilon: float = 0.01,
) -> list[Transition]:
    """Decide the registry transitions for one agent pass. Pure → unit-testable.

    Rules (the whole lifecycle policy lives here):
      * A source already in the feed (``has clusters``) but unknown to the registry is grandfathered
        straight to ``active`` — never hide something that is showing today.
      * A genuinely new source (seen, but no clusters yet) is ``registered`` — pending, held out of
        the feed until its articles corroborate.
      * A pending source whose articles have since corroborated (``has clusters``) is activated +
        scored.
      * An ``active`` source whose reputation has moved materially gets a refreshed score (so the
        console reflects current standing) — no event when nothing changed, to avoid log spam.
    """
    with_clusters = set(sources_with_clusters)
    out: list[Transition] = []
    for src in sorted(set(sources_seen)):
        if not src:
            continue
        has_cluster = src in with_clusters
        rep = reputation_by_source.get(src)
        prov = provider_by_source.get(src, "")
        rec = records.get(src)
        if rec is None:
            state = ACTIVE if has_cluster else REGISTERED
            out.append(Transition(src, state, rep if state == ACTIVE else None, prov, is_new=True))
        elif rec.state in PENDING_STATES:
            if has_cluster:
                out.append(Transition(src, ACTIVE, rep, prov or rec.provider, is_new=False))
            # else: still pending — leave it (part 2 moves registered→backfilling on a backfill run)
        else:  # ACTIVE: refresh reputation only when it has moved enough to matter
            if rep is not None and (rec.reputation is None or abs(rep - rec.reputation) >= rep_epsilon):
                out.append(Transition(src, ACTIVE, rep, prov or rec.provider, is_new=False))
    return out
