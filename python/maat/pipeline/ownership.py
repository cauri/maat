"""Source ownership resolution + grouping (#41 / #254) — pure logic, no I/O.

Given Wikidata claim data (fetched by ``acquire.wikidata``), pick the entity for a source, read its
DIRECT controlling owners (parent organization P749 ∪ owned by P127), and group sources that share a
controlling owner into one ownership group — the group corroboration collapses to a single
independent originator.

CONSERVATIVE by design: only DIRECT owners (no deep-conglomerate walk in v1), and a source with no
shared owner stays independent. A wrong merge HIDES real corroboration (makes a true fact look
thinner), so when in doubt we do NOT collapse. Operator ``admin.source.grouped`` overrides this
(handled where the maps are merged).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

from maat.pipeline.identity import canonical_source


def domain_of(url_or_host: str) -> str:
    """Registrable-ish host from a URL or host string ('https://www.reuters.com/x' → 'reuters.com')."""
    s = (url_or_host or "").split("//")[-1].split("/")[0].strip().lower()
    return s[4:] if s.startswith("www.") else s


def pick_entity(candidates: list[Mapping], source_domain: str, claims_by_qid: Mapping) -> str | None:
    """Choose the Wikidata entity for a source.

    Prefer a candidate whose official site (P856) domain matches the source's domain — the
    unambiguous signal. Otherwise fall back to the top search hit (Wikidata ranks the most-notable
    first). None when there are no candidates.
    """
    if source_domain:
        for c in candidates:
            sites = claims_by_qid.get(c["id"], {}).get("P856", []) or []
            if any(domain_of(u) == source_domain for u in sites if isinstance(u, str)):
                return c["id"]
    return candidates[0]["id"] if candidates else None


# Passive/index asset managers (#423). Wikidata's P127 ("owned by") does not distinguish CONTROL
# from "holds an index position", so these funds — which hold a slice of nearly every large public
# company — leak in as if they were parents. Observed live: `BlackRock -> ubs.com, morganstanley.com,
# ibm.com` became one "ownership group", meaning those three would collapse to a single independent
# originator and any fact all three reported would count ONCE. That is the same corroboration
# SUPPRESSION as the historical-owner chaining (#419/#422), from a different direction.
#
# Deliberately NOT here: Berkshire Hathaway (Q217583) and other holding companies that genuinely
# CONTROL their subsidiaries — if one of those owns two outlets, they really are one originator.
# The line is control, not shareholding.
_INSTITUTIONAL_INVESTORS: frozenset[str] = frozenset({
    "Q219635",     # BlackRock
    "Q849363",     # The Vanguard Group
    "Q2037125",    # State Street Corporation
    "Q1411292",    # Fidelity Investments
    "Q505275",     # Capital Group Companies
    "Q3511946",    # T. Rowe Price
    "Q105773164",  # Geode Capital Management
    "Q522617",     # Invesco
})


def direct_owners(claims: Mapping) -> list[str]:
    """An entity's direct CONTROLLING owners: parent-org (P749) ∪ owned-by (P127), order-stable.

    P749 (parent organization) is the control relation and is taken as-is. P127 (owned by) is
    where passive shareholders leak in, so index/asset managers are dropped from it (#423) — an
    outlet is not co-owned with every other company BlackRock holds a stake in. Erring toward NOT
    collapsing is the safe direction: a wrong merge HIDES real corroboration."""
    seen: set[str] = set()
    out: list[str] = []
    for q in [*(claims.get("P749") or []), *(claims.get("P127") or [])]:
        if not (isinstance(q, str) and q.startswith("Q")) or q in seen:
            continue
        if q in _INSTITUTIONAL_INVESTORS:
            continue  # a shareholding is not control — never a co-ownership link
        seen.add(q)
        out.append(q)
    return out


def fold_ownership(resolved: Iterable[Mapping]) -> dict[str, str]:
    """`source.ownership.resolved` events → ``{canonical_source: group_label}``.

    Sources that share any controlling owner are unioned into one group; the group's label is its
    most-common shared owner's name. A source that shares an owner with no one else stays out of the
    map (independent) — so a lone outlet is never "grouped" with itself.
    """
    canon_owners: dict[str, set[str]] = {}
    owner_label: dict[str, str] = {}
    for e in resolved:
        c = e.get("canonical") or canonical_source(e.get("source", ""))
        if not c:
            continue
        qs: set[str] = set()
        for o in e.get("owners") or []:
            qid = o.get("qid") if isinstance(o, Mapping) else o
            if isinstance(qid, str) and qid:
                qs.add(qid)
                lbl = o.get("label") if isinstance(o, Mapping) else None
                if lbl:
                    owner_label[qid] = lbl
        if qs:
            canon_owners.setdefault(c, set()).update(qs)

    sources = list(canon_owners)
    parent = {c: c for c in sources}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_owner: dict[str, list[str]] = {}
    for c, qs in canon_owners.items():
        for q in qs:
            by_owner.setdefault(q, []).append(c)
    for members in by_owner.values():
        for m in members[1:]:
            parent[find(members[0])] = find(m)

    components: dict[str, list[str]] = {}
    for c in sources:
        components.setdefault(find(c), []).append(c)

    out: dict[str, str] = {}
    for members in components.values():
        if len(members) < 2:
            continue  # alone → independent (a group needs ≥2 co-owned outlets)
        owners = Counter(q for m in members for q in canon_owners[m])
        top = owners.most_common(1)[0][0]
        label = owner_label.get(top, top)
        for m in members:
            out[m] = label
    return out


# --- evidence-gated collapse (#425): co-ownership is a hypothesis, not a merge --------------------

# Same-fact co-occurrences a co-owned pair needs before it collapses (when no shared REFUTED fact
# exists — one of those collapses the pair on its own). DRAFT knob.
_MIN_SHARED_FACTS = 2


def evidenced_ownership(
    auto: Mapping[str, str], history: Iterable[Mapping], *, min_shared: int = _MIN_SHARED_FACTS,
) -> dict[str, str]:
    """Keep only the co-owned pairs that demonstrably SHARE OUTPUT (#425) — cauri's design.

    "Co-owned outlets that make the same false claims — clear evidence of news laundering. Two
    outlets that do not share the same news (the Condé Nast pattern) need not be collapsed. State
    control is just like any other ownership."

    ``auto`` is ``fold_ownership``'s blanket map (co-ownership per Wikidata); ``history`` is the
    trajectory (``load_trajectory`` rows). A co-owned PAIR survives when:
      * it shares at least one fact that resolved REFUTED — the same false claim from commonly
        owned outlets is the strongest laundering signal, sufficient on its own; or
      * it co-occurs on ``min_shared`` or more same-fact clusters — shared output, the workable
        tier while refutations stay rare (no automated contradiction detector feeds outcomes yet).
    Surviving pairs re-union-find; components keep their group label; everything else DROPS OUT of
    the map — a co-owned outlet with no shared output counts as the independent originator it
    demonstrably is. A wrong merge HIDES real corroboration, so the doubt resolves to independence
    (the module's standing posture) — while the CONTENT-based collapse signals (lexical
    near-duplication, citation cascade, wire credit) still catch verbatim laundering immediately,
    with or without ownership: ownership was the only content-blind edge, and now it needs evidence.

    Operator ``admin.source.grouped`` entries are merged OVER this map by the callers, unchanged —
    a human's explicit group never needs statistical evidence.
    """
    if not auto:
        return {}
    # Which fact-buckets did each co-owned source appear in, and how did each fact resolve?
    from maat.learning.calibration import REFUTED, resolve_outcome  # local: avoid an import cycle

    by_fact: dict[str, list[Mapping]] = {}
    for ev in history:
        key = " ".join(str(ev.get("fact", "")).lower().split())
        if key:
            by_fact.setdefault(key, []).append(ev)

    pair_shared: dict[tuple[str, str], int] = {}
    pair_refuted: dict[tuple[str, str], int] = {}
    for hist in by_fact.values():
        first, last = hist[0], hist[-1]
        outcome = resolve_outcome(
            int(first.get("independent_originators", 0)),
            int(last.get("independent_originators", 0)),
            latest_has_primary=bool(last.get("has_primary", False)),
            corrected=any(h.get("corrected") for h in hist),
            grounding=last.get("grounding"),
        )
        # Trajectory sources are RAW strings; the ownership map is keyed canonical (#36's lesson).
        canons = sorted({
            c for s in (last.get("sources") or []) if (c := canonical_source(str(s))) in auto
        })
        for i, a in enumerate(canons):
            for b in canons[i + 1:]:
                if auto.get(a) != auto.get(b):
                    continue  # different owners — sharing a fact is just corroboration
                pair_shared[(a, b)] = pair_shared.get((a, b), 0) + 1
                if outcome == REFUTED:
                    pair_refuted[(a, b)] = pair_refuted.get((a, b), 0) + 1

    survivors = [
        pair for pair, n in pair_shared.items()
        if pair_refuted.get(pair, 0) >= 1 or n >= min_shared
    ]
    if not survivors:
        return {}
    # Union-find over the surviving pairs only — a blanket group can SPLIT into evidenced subgroups.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in survivors:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for c in parent:
        groups.setdefault(find(c), []).append(c)
    out: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        label = Counter(auto[m] for m in members).most_common(1)[0][0]
        for m in members:
            out[m] = label
    return out
