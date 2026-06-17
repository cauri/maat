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


def direct_owners(claims: Mapping) -> list[str]:
    """An entity's direct controlling owners: parent-org (P749) ∪ owned-by (P127), order-stable."""
    seen: set[str] = set()
    out: list[str] = []
    for q in [*(claims.get("P749") or []), *(claims.get("P127") or [])]:
        if isinstance(q, str) and q.startswith("Q") and q not in seen:
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
