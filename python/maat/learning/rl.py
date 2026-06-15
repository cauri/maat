"""RL loop: offline policy-improvement step that ties the veracity signals together (P3, §8, #41).

A `Policy` bundles the tunable confidence weights (`calibration.Weights`) with learned source
preferences derived from the reputation fold (`reputation.SourceReputation`). A policy_step()
proposes an improved policy — reusing calibration.tune_decay/replay_ab for the weight side and
reputation for the source-preference side — strictly BOUNDED and returned as a SIGN-OFF-GATED
proposal record, never auto-applied. The justification comes from an A/B-on-replay run.

This is Gamelan's "bounded self-modification" pattern: the system may propose changes within a
safe envelope, audited and operator-approved; it may never silently escalate. The same guardrail
calibration.py enforces is applied here at the policy level.

`ownership_graph(edges)` folds operator-provided or derived ownership links into clusters so
co-owned outlets are NOT counted as independent originators. Given a set of sources and their
ownership edges, it returns the ownership-collapsed groups: each group is a frozenset of sources
whose outlets share an owner, so callers can collapse them before counting independent originators.

Pure functions over plain values — no DB, no I/O, deterministic.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from maat.learning.calibration import (
    Observation,
    ReplayAB,
    Weights,
    replay_ab,
    tune_decay,
    tune_proposals,
)
from maat.learning.reputation import SourceReputation, fold_reputation


# ---------------------------------------------------------------------------
# Policy — the tunable object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    """The tunable policy: confidence weights + learned source preferences.

    `weights`           — the per-extremity decay constants, primary_lift, and cap; the
                         same `Weights` object that `calibration.tune_decay` proposes changes to.
    `source_preference` — a mapping from source name → preference score in [0.0, 1.0].
                         Higher = prefer this source when multiple originators cover the same fact.
                         Derived from `SourceReputation.confirmation_rate` (or independent_rate
                         if no resolved outcomes exist yet). Absent sources are implicitly 0.5
                         (neutral). NOT used to suppress facts — only as a soft preference signal.
    """

    weights: Weights
    source_preference: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def default(cls) -> Policy:
        return cls(weights=Weights.defaults(), source_preference={})


# ---------------------------------------------------------------------------
# Bounds on the source-preference adjustment (Gamelan bounded self-modification)
# ---------------------------------------------------------------------------

# Source preference values must stay within [0.0, 1.0]; a single step cannot move more
# than this fraction in either direction from the base value.
_PREF_FLOOR: float = 0.0
_PREF_CEIL: float = 1.0
_PREF_MAX_DELTA: float = 0.30  # one step can shift preference at most 0.30


def _clamp_preference(base: float, proposed: float) -> float:
    """Enforce the Gamelan envelope on a single source-preference update.

    The proposed value must (a) stay within [PREF_FLOOR, PREF_CEIL], and (b) not move more
    than PREF_MAX_DELTA from the base. Returns the clamped value.
    """
    lo = max(_PREF_FLOOR, base - _PREF_MAX_DELTA)
    hi = min(_PREF_CEIL, base + _PREF_MAX_DELTA)
    return round(min(hi, max(lo, proposed)), 4)


def _preference_from_reputation(rep: SourceReputation) -> float:
    """Derive a preference score ∈ [0, 1] from a reputation record.

    Primary signal: confirmation_rate (fraction of resolved facts that confirmed).
    Fallback: independent_rate (fraction of appearances as an independent originator).
    Both are in [0, 1] and already reflect truth-over-time, not consensus.
    """
    if rep.confirmation_rate is not None:
        return rep.confirmation_rate
    return rep.independent_rate


# ---------------------------------------------------------------------------
# Policy proposal record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PolicyProposal:
    """A bounded, sign-off-gated proposal to improve the current policy.

    Fields
    ------
    candidate       — the proposed policy (NEVER auto-applied; requires operator sign-off).
    ab              — the A/B-on-replay result justifying the weight side of the proposal.
    weight_changes  — list of ``{key, value, reason}`` dicts targeting the Config registry,
                     exactly as `calibration.tune_proposals` emits — empty if no decay change.
    pref_changes    — list of ``{source, before, after, reason}`` dicts for source preferences
                     that changed within the safe envelope — empty if no preference changed.
    n_observations  — number of resolved observations the weight side was evaluated on.
    approved        — always False; only an operator can flip this to True (sign-off gate).
    """

    candidate: Policy
    ab: ReplayAB
    weight_changes: list[dict]
    pref_changes: list[dict]
    n_observations: int
    approved: bool = False


# ---------------------------------------------------------------------------
# Core: policy_step
# ---------------------------------------------------------------------------

def policy_step(
    history: Iterable[Mapping],
    reputation: Iterable[SourceReputation] | None = None,
    base_policy: Policy | None = None,
) -> PolicyProposal:
    """Offline policy-improvement step — proposes a better policy, never applies it.

    Algorithm
    ---------
    1. WEIGHTS: call `calibration.tune_decay` on the resolved observation history to search
       for better per-extremity decay constants. Evaluate with `calibration.replay_ab` to
       produce an A/B-on-replay justification. Changes are bounded by calibration's own
       _DECAY_FLOOR/_DECAY_CEIL/_MAX_DELTA envelope.
    2. SOURCE PREFERENCES: from each SourceReputation, derive a preference score and clamp
       it within `_PREF_MAX_DELTA` of the base value. Sources with no reputation record keep
       their base preference (or 0.5 default if absent in base_policy too).
    3. Assemble a `PolicyProposal` with candidate policy + full audit trail. `approved` is
       always False — the operator must explicitly sign off before any change goes live.

    Parameters
    ----------
    history     — stream of `cluster.corroborated` event dicts (oldest → newest) from which
                  `Observation` records are derived for the weight calibration step.
    reputation  — pre-computed SourceReputation list, or None to derive it from `history`.
                  Pass pre-computed if you have already folded the reputation separately.
    base_policy — the current policy to improve from; defaults to `Policy.default()`.

    Returns
    -------
    PolicyProposal — always a PROPOSAL, never auto-applied.
    """
    base = base_policy or Policy.default()
    hist = list(history)

    # --- derive reputation if not supplied ---
    if reputation is None:
        reputation = fold_reputation(hist)
    rep_list = list(reputation)

    # --- observations (from calibration) ---
    from maat.learning.calibration import observations_from_history
    obs: list[Observation] = observations_from_history(hist)

    # --- weight step ---
    tuned_weights, _tuned_brier = tune_decay(obs, base=base.weights)
    ab = replay_ab(obs, base=base.weights, candidate=tuned_weights)
    weight_changes = tune_proposals(obs, base=base.weights)

    # --- source-preference step ---
    base_prefs: dict[str, float] = dict(base.source_preference)
    candidate_prefs: dict[str, float] = dict(base_prefs)
    pref_changes: list[dict] = []

    for rep in rep_list:
        proposed_raw = _preference_from_reputation(rep)
        base_pref = base_prefs.get(rep.source, 0.5)
        clamped = _clamp_preference(base_pref, proposed_raw)
        if clamped != base_pref:
            candidate_prefs[rep.source] = clamped
            pref_changes.append({
                "source": rep.source,
                "before": base_pref,
                "after": clamped,
                "reason": (
                    f"reputation: confirmation_rate={rep.confirmation_rate}, "
                    f"independent_rate={rep.independent_rate:.3f}, "
                    f"appearances={rep.appearances}"
                ),
            })
        else:
            # Preference unchanged (already in bounds or at clamped boundary)
            if rep.source not in candidate_prefs:
                candidate_prefs[rep.source] = clamped

    candidate = Policy(weights=tuned_weights, source_preference=candidate_prefs)

    # scalar n_observations: number of scored (terminal) observations
    from maat.learning.calibration import _scorable
    n_observations = len(_scorable(obs))

    return PolicyProposal(
        candidate=candidate,
        ab=ab,
        weight_changes=weight_changes,
        pref_changes=pref_changes,
        n_observations=n_observations,
        approved=False,  # NEVER auto-approved
    )


# ---------------------------------------------------------------------------
# Ownership graph — collapse co-owned outlets before counting independent originators
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OwnershipGroup:
    """A cluster of sources that share ownership.

    `sources` is the frozenset of outlet/source names that are under common ownership.
    A singleton frozenset means the source has no ownership link to any other source in
    the given edge set and is considered fully independent.
    """

    sources: frozenset[str]


def ownership_graph(edges: Iterable[tuple[str, str]]) -> list[OwnershipGroup]:
    """Fold operator-provided ownership edges into ownership-collapsed clusters.

    Sources connected by an ownership edge (direct or transitive) are placed in the same
    group and should NOT be counted as independent originators of a fact — they are
    co-owned outlets. Sources not connected to any other source form singleton groups and
    ARE independent.

    Parameters
    ----------
    edges — pairs of (source_a, source_b) meaning "source_a and source_b share an owner".
            Both directions of each edge are implicitly included. Self-edges are ignored.
            Transitivity is handled: if A–B and B–C are edges, then {A, B, C} is one group.

    Returns
    -------
    List of `OwnershipGroup` objects — one per connected component in the ownership graph.
    The order is deterministic (sorted by min-source-name within group, then groups sorted
    by their min-source-name) for stable comparisons in tests and diffs.

    Note: isolated sources (those only mentioned in edges, not forming multi-member groups)
    that appear in the edge list DO end up grouped only if they share an edge.  Sources that
    do NOT appear in the edge list at all are not returned — pass them explicitly if you want
    singleton groups for independent sources.
    """
    edge_list = list(edges)

    # Build adjacency: collect all nodes first.
    adjacency: dict[str, set[str]] = {}
    for a, b in edge_list:
        if a == b:
            continue  # self-edge: ignore
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    if not adjacency:
        return []

    # Union-find over the node set.
    nodes = list(adjacency)
    parent: dict[str, str] = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for a, neighbours in adjacency.items():
        for b in neighbours:
            union(a, b)

    # Group nodes by root.
    clusters: dict[str, list[str]] = {}
    for n in nodes:
        root = find(n)
        clusters.setdefault(root, []).append(n)

    # Build OwnershipGroup objects, sorted deterministically.
    groups = [
        OwnershipGroup(sources=frozenset(members))
        for members in clusters.values()
    ]
    groups.sort(key=lambda g: sorted(g.sources)[0])
    return groups


def collapse_by_ownership(
    sources: Iterable[str], groups: Iterable[OwnershipGroup]
) -> list[frozenset[str]]:
    """Map a set of sources through ownership groups to get their collapsed clusters.

    Given a set of sources that appear in a corroboration cluster AND a list of ownership
    groups (from `ownership_graph`), returns the de-duplicated ownership-collapsed clusters
    that are actually represented. Sources not in any ownership group are treated as singletons
    (independent). The caller counts `len(result)` as the true independent-originator count.

    Parameters
    ----------
    sources — source names present in the corroboration cluster.
    groups  — ownership groups from `ownership_graph`.

    Returns
    -------
    List of frozensets; each frozenset is one "ownership-independent" originator. The length
    of the list is the count of true independent originators after ownership collapse.
    """
    src_set = set(sources)
    if not src_set:
        return []

    # Build a lookup: source → its ownership group
    group_for: dict[str, frozenset[str]] = {}
    for g in groups:
        for s in g.sources:
            group_for[s] = g.sources

    # For each source in the cluster, find its collapsed group.
    seen_roots: set[frozenset[str]] = set()
    result: list[frozenset[str]] = []
    for s in sorted(src_set):  # sorted for determinism
        group = group_for.get(s, frozenset({s}))  # singleton if no ownership link
        # Intersect with actual sources in the cluster — we only count the sub-group present.
        present = frozenset(group & src_set) or frozenset({s})
        if present not in seen_roots:
            seen_roots.add(present)
            result.append(present)

    return result
