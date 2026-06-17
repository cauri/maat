"""Source registry + lifecycle (#241) — fold, transition planner, and the fail-open feed gate."""

from __future__ import annotations

from maat.learning.source_registry import (
    ACTIVE,
    REGISTERED,
    SCORED,
    active_sources,
    fold_sources,
    pending_sources,
    plan_registry,
)
from maat.serving.feed import _filter_pending


def test_fold_applies_in_order_with_first_seen_fixed():
    recs = fold_sources([
        {"source": "bbc.com", "state": REGISTERED, "provider": "rss", "at": "t1"},
        {"source": "bbc.com", "state": ACTIVE, "reputation": 0.7, "at": "t2"},
        {"source": "kremlin.ru", "state": REGISTERED, "at": "t3"},
    ])
    assert recs["bbc.com"].state == ACTIVE
    assert recs["bbc.com"].reputation == 0.7
    assert recs["bbc.com"].provider == "rss"           # carried from the first event
    assert recs["bbc.com"].first_registered_at == "t1"  # fixed at first sighting
    assert recs["bbc.com"].last_changed_at == "t2"
    assert recs["kremlin.ru"].state == REGISTERED


def test_pending_and_active_partition():
    recs = fold_sources([
        {"source": "a", "state": ACTIVE, "at": "t"},
        {"source": "b", "state": REGISTERED, "at": "t"},
        {"source": "c", "state": SCORED, "at": "t"},
    ])
    assert active_sources(recs) == {"a"}
    assert pending_sources(recs) == {"b", "c"}  # everything short of active is held


def test_plan_grandfathers_feed_sources_and_holds_new_ones():
    # Empty registry; two sources seen. One already has clusters (in the feed), one brand new.
    plan = plan_registry(
        records={},
        sources_seen=["inthefeed.com", "brandnew.com"],
        provider_by_source={"inthefeed.com": "rss", "brandnew.com": "apify-locale"},
        sources_with_clusters=["inthefeed.com"],
        reputation_by_source={"inthefeed.com": 0.62},
    )
    by_src = {t.source: t for t in plan}
    # in the feed today -> grandfathered straight to active (never hide something already showing)
    assert by_src["inthefeed.com"].state == ACTIVE
    assert by_src["inthefeed.com"].is_new and by_src["inthefeed.com"].reputation == 0.62
    # genuinely new -> registered (pending), held out of the feed, no reputation yet
    assert by_src["brandnew.com"].state == REGISTERED
    assert by_src["brandnew.com"].reputation is None


def test_plan_activates_pending_source_once_it_corroborates():
    recs = fold_sources([{"source": "newswire.example", "state": REGISTERED, "at": "t"}])
    plan = plan_registry(
        records=recs,
        sources_seen=["newswire.example"],
        provider_by_source={"newswire.example": "rss"},
        sources_with_clusters=["newswire.example"],   # its articles have now corroborated
        reputation_by_source={"newswire.example": 0.4},
    )
    assert len(plan) == 1
    assert plan[0].state == ACTIVE and not plan[0].is_new and plan[0].reputation == 0.4


def test_plan_is_idempotent_when_nothing_changed():
    recs = fold_sources([
        {"source": "a", "state": ACTIVE, "reputation": 0.5, "at": "t"},
    ])
    # active, reputation unchanged, still has clusters -> no event
    plan = plan_registry(
        records=recs,
        sources_seen=["a"],
        provider_by_source={},
        sources_with_clusters=["a"],
        reputation_by_source={"a": 0.5},
    )
    assert plan == []


def test_plan_refreshes_active_reputation_only_when_it_moves():
    recs = fold_sources([{"source": "a", "state": ACTIVE, "reputation": 0.50, "at": "t"}])
    common = dict(records=recs, sources_seen=["a"], provider_by_source={}, sources_with_clusters=["a"])
    assert plan_registry(**common, reputation_by_source={"a": 0.504}) == []         # within epsilon
    moved = plan_registry(**common, reputation_by_source={"a": 0.62})
    assert len(moved) == 1 and moved[0].state == ACTIVE and moved[0].reputation == 0.62


def test_pending_source_without_clusters_stays_pending():
    recs = fold_sources([{"source": "slow.example", "state": REGISTERED, "at": "t"}])
    plan = plan_registry(
        records=recs,
        sources_seen=["slow.example"],
        provider_by_source={},
        sources_with_clusters=[],          # not corroborated yet
        reputation_by_source={},
    )
    assert plan == []  # left pending — held out of the feed until it corroborates


def test_feed_gate_is_fail_open_and_drops_pending_only_stories():
    payload = {
        "stories": [
            {"id": "1", "originator_groups": [{"sources": ["pending.example"]}]},
            {"id": "2", "originator_groups": [{"sources": ["pending.example", "active.example"]}]},
            {"id": "3", "originator_groups": [{"sources": ["unregistered.example"]}]},
        ],
        "count": 3,
    }
    # empty pending set -> nothing hidden (fail-open)
    assert _filter_pending(payload, set())["count"] == 3
    out = _filter_pending(payload, {"pending.example"})
    kept = {s["id"] for s in out["stories"]}
    assert kept == {"2", "3"}          # story 1 (pending-only) dropped; mixed + unregistered kept
    assert out["count"] == 2
