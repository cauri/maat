"""Content-addressed id helpers (#289).

One home for every stable, content-derived id in the system. These are
content-addresses, **not** security hashes: SHA1 is deliberate, and the digest plus
truncation length MUST stay byte-for-byte stable — change either and every id already
in the store churns, orphaning the rows that reference the old id.

``usedforsecurity=False`` documents that intent (and silences the false-positive
insecure-hash linters); it does **not** change the digest. ``tests/test_ids.py`` pins
every helper against its original inline formula so the bytes can never drift.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable


def _addr(payload: str, length: int) -> str:
    """First ``length`` hex chars of sha1(payload) — content-addressing, never security."""
    return hashlib.sha1(payload.encode(), usedforsecurity=False).hexdigest()[:length]


def text_fingerprint(text: str) -> str:
    """Full sha256 hex of ``text`` — a collision-safe content key for the embedding cache (#286).

    Untruncated (unlike the short ids above): a collision here would reuse the WRONG vector and
    silently corrupt clustering, so trade the extra bytes for sha256's full collision resistance.
    """
    return hashlib.sha256(text.encode(), usedforsecurity=False).hexdigest()


def article_id(url: str, prefix: str) -> str:
    """``<prefix>-<sha1(url)[:18]>`` — one id per acquisition channel (rss/gd/cc/nd/loc/bf...)."""
    return f"{prefix}-" + _addr(url, 18)


def cluster_id(claim_ids: Iterable[str]) -> str:
    """Stable cluster id = hash of its member claim ids; order-independent, membership-sensitive."""
    return _addr("|".join(sorted(claim_ids)), 24)


def node_id(entity_spine: Iterable[str], cluster_id: str) -> str:
    """Story-graph node id: the sorted entity spine joined to the cluster id (spike §1)."""
    payload = "|".join(sorted(entity_spine)) + ":" + cluster_id
    return "node:" + _addr(payload, 16)


def relation_id(a: str, b: str, relation: str) -> str:
    """Stable id for an unordered claim pair + relation, so the kernel dedups re-runs."""
    key = "|".join([*sorted((a, b)), relation])
    return "rel-" + _addr(key, 20)


def snapshot_id(cluster_id: str, harvest_date: str) -> str:
    """Stable (cluster, calendar-date) snapshot id — same day → same id, no duplicate events."""
    return "snap-" + _addr(f"{cluster_id}:{harvest_date}", 18)


def backfill_run_id(source: str, stamp: str) -> str:
    """Stable backfill run id from a source + caller-supplied timestamp (callers pass the time)."""
    return "bf-" + _addr(f"{source}|{stamp}", 16)


def issue_dedup_key(category: str, base: str) -> str:
    """``<category>::<sha1(base)[:12]>`` — collapses near-identical feedback into one issue."""
    return f"{category}::{_addr(base, 12)}"
