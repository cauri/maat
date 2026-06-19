"""#289 — maat/ids.py content-addressing.

The contract is byte-for-byte stability: these ids are stored content-addresses, so each
helper must reproduce the EXACT inline sha1 formula it replaced. Every test recomputes the
original formula by hand and asserts equality — if anyone ever changes a prefix, the digest,
or a truncation length, these break before the drift can orphan stored rows.
"""

import hashlib

from maat import ids


def _sha1(payload: str) -> str:
    """The original inline form — deliberately NOT routed through ids.py."""
    return hashlib.sha1(payload.encode()).hexdigest()


def test_usedforsecurity_flag_does_not_change_digest():
    # The whole byte-for-byte guarantee rests on this: the flag is advisory, not a digest change.
    assert ids._addr("anything", 40) == _sha1("anything")


def test_article_id_matches_inline_formula():
    url = "https://example.com/world/2026/news?id=42&x=y"
    for prefix in ("rss", "gd", "cc", "loc", "nd", "bf"):
        assert ids.article_id(url, prefix) == prefix + "-" + _sha1(url)[:18]


def test_cluster_id_matches_inline_formula():
    claim_ids = ["claim-c", "claim-a", "claim-b"]
    assert ids.cluster_id(claim_ids) == _sha1("|".join(sorted(claim_ids)))[:24]


def test_cluster_id_order_independent_membership_sensitive():
    assert ids.cluster_id(["b", "a"]) == ids.cluster_id(["a", "b"])
    assert ids.cluster_id(["a", "b"]) != ids.cluster_id(["a", "c"])


def test_node_id_matches_inline_formula():
    spine = ["Mali", "ECOWAS", "Sahel"]
    cid = "abc123def456"
    payload = "|".join(sorted(spine)) + ":" + cid
    assert ids.node_id(spine, cid) == "node:" + _sha1(payload)[:16]


def test_relation_id_matches_inline_formula_and_is_unordered():
    a, b, rel = "claim-x", "claim-y", "contradicts"
    key = "|".join([*sorted((a, b)), rel])
    assert ids.relation_id(a, b, rel) == "rel-" + _sha1(key)[:20]
    assert ids.relation_id(a, b, rel) == ids.relation_id(b, a, rel)  # pair is unordered
    assert ids.relation_id(a, b, "contradicts") != ids.relation_id(a, b, "entails")


def test_snapshot_id_matches_inline_formula():
    cid, date = "clust-abc123", "2026-06-15"
    assert ids.snapshot_id(cid, date) == "snap-" + _sha1(f"{cid}:{date}")[:18]


def test_backfill_run_id_matches_inline_formula():
    src, stamp = "bbc.com", "2026-06-19T00:00:00Z"
    assert ids.backfill_run_id(src, stamp) == "bf-" + _sha1(f"{src}|{stamp}")[:16]


def test_issue_dedup_key_matches_inline_formula():
    cat, base = "ui", "login button does nothing"
    assert ids.issue_dedup_key(cat, base) == f"{cat}::{_sha1(base)[:12]}"


def test_helpers_preserve_known_prefixes_and_lengths():
    # Length/prefix guard: a fat-finger on a truncation slice trips here too.
    assert ids.article_id("u", "rss").startswith("rss-") and len(ids.article_id("u", "rss")) == 4 + 18
    assert len(ids.cluster_id(["a"])) == 24
    assert ids.node_id(["a"], "c").startswith("node:") and len(ids.node_id(["a"], "c")) == 5 + 16
    assert ids.relation_id("a", "b", "r").startswith("rel-") and len(ids.relation_id("a", "b", "r")) == 4 + 20
    assert ids.snapshot_id("c", "d").startswith("snap-") and len(ids.snapshot_id("c", "d")) == 5 + 18
    assert ids.backfill_run_id("s", "t").startswith("bf-") and len(ids.backfill_run_id("s", "t")) == 3 + 16
