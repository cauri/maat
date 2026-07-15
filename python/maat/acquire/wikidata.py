"""Wikidata lookup for source ownership (#41 / #254, integrity) — I/O only.

A thin httpx client over the Wikidata API: resolve an outlet name to candidate entities, and read an
entity's ownership claims (parent organization P749 / owned by P127), instance-of (P31), and official
site (P856, for domain disambiguation). No API key; Wikidata requires a descriptive User-Agent.

The pure resolution + grouping logic lives in `pipeline/ownership.py` and is tested without the
network; this module is the seam the ownership agent calls (and tests mock).
"""

from __future__ import annotations

import httpx

_API = "https://www.wikidata.org/w/api.php"
_UA = {"User-Agent": "MaatBot/0.1 (+https://maat.press; veracity research) httpx"}
_TIMEOUT = 20.0

# Wikidata property ids we read.
P_INSTANCE_OF = "P31"
P_PARENT_ORG = "P749"
P_OWNED_BY = "P127"
P_OFFICIAL_SITE = "P856"
_CLAIM_PROPS = (P_INSTANCE_OF, P_PARENT_ORG, P_OWNED_BY, P_OFFICIAL_SITE)


def search_entities(name: str, *, limit: int = 5) -> list[dict]:
    """Candidate entities for a name: ``[{id, label, description}]``. ``[]`` on anything wrong."""
    if not name.strip():
        return []
    try:
        r = httpx.get(
            _API,
            params={"action": "wbsearchentities", "search": name, "language": "en",
                    "format": "json", "type": "item", "limit": limit},
            headers=_UA, timeout=_TIMEOUT,
        )
        return [
            {"id": x["id"], "label": x.get("label", ""), "description": x.get("description", "")}
            for x in r.json().get("search", [])
        ]
    except Exception:
        return []


def _is_historical(claim: dict) -> bool:
    """Is this Wikidata statement about the PAST — a former owner rather than a current one? (#419)

    Wikidata records ownership history on the same property: "AOL — owned by — WarnerMedia" stays on
    the entity forever, with a P582 ("end time") qualifier marking when it ended. Reading only the
    mainsnak (as this did) treats every former parent as a current one — and that is not a harmless
    over-count, it CHAINS: WarnerMedia is a former owner of aol.co.uk, tmz.com AND cnnturk.com, so
    it bridged Yahoo → AOL → TMZ → Fox News → NY Post → CNN Türk → Milliyet into one "ownership
    group". Those 7 outlets would have collapsed to a single independent originator, so a story all
    of them reported would count once — silently suppressing real corroboration, the exact failure
    the ownership rollup exists to prevent in the other direction.

    Skipped: a statement with an end time (ownership that has ended), and a `deprecated`-rank
    statement (Wikidata's own marker for "known wrong")."""
    if claim.get("rank") == "deprecated":
        return True
    return bool(claim.get("qualifiers", {}).get("P582"))  # P582 = end time → former


def entity_claims(qid: str) -> dict:
    """An entity's label + the claim ids we read: ``{label, P31, P749, P127, P856}``. ``{}`` on error.

    CURRENT statements only — former/deprecated ones are dropped (see ``_is_historical``)."""
    try:
        r = httpx.get(
            _API,
            params={"action": "wbgetentities", "ids": qid, "props": "claims|labels",
                    "languages": "en", "format": "json"},
            headers=_UA, timeout=_TIMEOUT,
        )
        ent = r.json()["entities"][qid]
        out: dict = {"label": ent.get("labels", {}).get("en", {}).get("value", "")}
        for p in _CLAIM_PROPS:
            vals = []
            for c in ent.get("claims", {}).get(p, []):
                if _is_historical(c):
                    continue  # a FORMER owner is not a co-ownership link today (#419)
                dv = c.get("mainsnak", {}).get("datavalue", {}).get("value")
                vals.append(dv.get("id") if isinstance(dv, dict) and "id" in dv else dv)
            out[p] = vals
        return out
    except Exception:
        return {}
