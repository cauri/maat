"""Tests for the tenancy model (P5, issue #46).

Three invariants under test:
  (A) Isolation holds — tenant A cannot read tenant B's personal state.
  (B) Shared truth is identical across tenants — same object, no divergence.
  (C) Cross-tenant access is rejected — TenantIsolationError is raised at every
      boundary function when a mismatched tenant_id is supplied.

All tests are pure — no DB, no I/O, no network, no async.
"""

from __future__ import annotations

import pytest

from maat.serving.tenancy import (
    SharedStory,
    SharedVeracityProjection,
    TenantIsolationError,
    TenantState,
    _assert_tenant,
    add_comment,
    build_tenant_view,
    empty_tenant_state,
    follow_topic,
    get_tenant_state,
    hide_story,
    mark_seen,
    merge_tenant_preference,
    pin_story,
    shared_truth_is_identical,
    visible_stories,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _story(
    id: str,
    confidence: float = 0.75,
    label: str = "corroborated",
    sources: list[str] | None = None,
    country: str = "US",
) -> SharedStory:
    return SharedStory(
        id=id,
        headline=f"Headline {id}",
        confidence=confidence,
        label=label,
        independent_originators=2,
        has_primary=True,
        extremity="notable",
        sources=sources or ["reuters"],
        country=country,
        language="en",
    )


def _projection(*stories: SharedStory) -> SharedVeracityProjection:
    return SharedVeracityProjection(
        stories_by_id={s.id: s for s in stories},
        ordered_ids=[s.id for s in stories],
        source_reputations={},
        calibration_snapshot={},
    )


def _state(tenant_id: str, **kwargs) -> TenantState:
    return TenantState(tenant_id=tenant_id, **kwargs)


# ---------------------------------------------------------------------------
# (A) ISOLATION: tenant A's personal state is invisible to tenant B
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_pinned_ids_are_per_tenant(self):
        """A story pinned by tenant A must NOT appear as pinned in tenant B's view."""
        shared = _projection(_story("s1"), _story("s2"))
        state_a = _state("alice", pinned_ids=frozenset({"s1"}))
        state_b = _state("bob")

        view_a = build_tenant_view("alice", shared, state_a)
        view_b = build_tenant_view("bob", shared, state_b)

        a_s1 = next(sv for sv in view_a.stories if sv.id == "s1")
        b_s1 = next(sv for sv in view_b.stories if sv.id == "s1")

        assert a_s1.is_pinned is True
        assert b_s1.is_pinned is False  # isolation: bob cannot see alice's pin

    def test_seen_ids_are_per_tenant(self):
        """A story marked seen by tenant A must NOT appear seen in tenant B's view."""
        shared = _projection(_story("x"))
        state_a = _state("alice", seen_ids=frozenset({"x"}))
        state_b = _state("bob")

        view_a = build_tenant_view("alice", shared, state_a)
        view_b = build_tenant_view("bob", shared, state_b)

        assert view_a.stories[0].is_seen is True
        assert view_b.stories[0].is_seen is False

    def test_hidden_ids_are_per_tenant(self):
        """A story hidden by tenant A is still visible to tenant B."""
        shared = _projection(_story("h1"), _story("h2"))
        state_a = _state("alice", hidden_ids=frozenset({"h1"}))
        state_b = _state("bob")

        view_a = build_tenant_view("alice", shared, state_a)
        view_b = build_tenant_view("bob", shared, state_b)

        a_visible = visible_stories(view_a)
        b_visible = visible_stories(view_b)

        a_ids = [sv.id for sv in a_visible]
        b_ids = [sv.id for sv in b_visible]

        assert "h1" not in a_ids    # hidden for alice
        assert "h1" in b_ids        # still visible for bob

    def test_comments_are_per_tenant(self):
        """A private comment left by tenant A must NOT appear in tenant B's view."""
        shared = _projection(_story("c1"))
        state_a = _state("alice", comments={"c1": "alice's private note"})
        state_b = _state("bob")

        view_a = build_tenant_view("alice", shared, state_a)
        view_b = build_tenant_view("bob", shared, state_b)

        assert view_a.stories[0].personal_comment == "alice's private note"
        assert view_b.stories[0].personal_comment == ""  # bob sees nothing

    def test_followed_topics_are_per_tenant(self):
        """followed_topics must live on the state, not bleed across tenants."""
        state_a = follow_topic("alice", _state("alice"), "climate")
        state_b = _state("bob")

        assert "climate" in state_a.followed_topics
        assert "climate" not in state_b.followed_topics

    def test_preferences_are_per_tenant(self):
        state_a = merge_tenant_preference("alice", _state("alice"), "density", "compact")
        state_b = _state("bob")

        assert state_a.preferences.get("density") == "compact"
        assert "density" not in state_b.preferences

    def test_get_tenant_state_only_returns_own_state(self):
        """get_tenant_state must never return a different tenant's state."""
        state_a = _state("alice")
        all_states = {"alice": state_a}

        result = get_tenant_state("alice", all_states)
        assert result.tenant_id == "alice"

    def test_get_tenant_state_missing_returns_empty(self):
        """Requesting a non-existent tenant returns an empty state, not another's."""
        state_a = _state("alice")
        all_states = {"alice": state_a}

        result = get_tenant_state("bob", all_states)
        assert result.tenant_id == "bob"
        assert len(result.pinned_ids) == 0
        assert len(result.seen_ids) == 0
        assert len(result.followed_topics) == 0


# ---------------------------------------------------------------------------
# (B) SHARED TRUTH: identical across tenants, not duplicated or diverged
# ---------------------------------------------------------------------------

class TestSharedTruth:
    def test_confidence_identical_across_tenants(self):
        """The confidence score for a story must be exactly equal for all tenants."""
        shared = _projection(_story("s1", confidence=0.87))
        view_a = build_tenant_view("alice", shared, _state("alice"))
        view_b = build_tenant_view("bob", shared, _state("bob"))

        a_conf = next(sv.confidence for sv in view_a.stories if sv.id == "s1")
        b_conf = next(sv.confidence for sv in view_b.stories if sv.id == "s1")

        assert a_conf == b_conf == 0.87

    def test_label_identical_across_tenants(self):
        """Corroboration label must be identical for all tenants."""
        shared = _projection(_story("s1", label="well corroborated"))
        view_a = build_tenant_view("alice", shared, _state("alice"))
        view_b = build_tenant_view("bob", shared, _state("bob"))

        a_label = next(sv.label for sv in view_a.stories if sv.id == "s1")
        b_label = next(sv.label for sv in view_b.stories if sv.id == "s1")

        assert a_label == b_label == "well corroborated"

    def test_shared_projection_id_is_same_object(self):
        """Both views must carry the same shared_projection_id — no copy was made."""
        shared = _projection(_story("s1"))
        view_a = build_tenant_view("alice", shared, _state("alice"))
        view_b = build_tenant_view("bob", shared, _state("bob"))

        assert view_a.shared_projection_id == view_b.shared_projection_id == id(shared)

    def test_shared_truth_is_identical_helper(self):
        """shared_truth_is_identical() returns True when both views share the projection."""
        shared = _projection(_story("s1"))
        view_a = build_tenant_view("alice", shared, _state("alice"))
        view_b = build_tenant_view("bob", shared, _state("bob"))

        assert shared_truth_is_identical(view_a, view_b, shared) is True

    def test_shared_truth_is_identical_fails_for_different_projections(self):
        """shared_truth_is_identical() returns False when views used different projections."""
        shared1 = _projection(_story("s1"))
        shared2 = _projection(_story("s1"))  # different object
        view_a = build_tenant_view("alice", shared1, _state("alice"))
        view_b = build_tenant_view("bob", shared2, _state("bob"))

        assert shared_truth_is_identical(view_a, view_b, shared1) is False

    def test_sources_identical_across_tenants(self):
        """Source list must be identical for both tenants."""
        shared = _projection(_story("s1", sources=["ap", "reuters", "bbc"]))
        view_a = build_tenant_view("alice", shared, _state("alice"))
        view_b = build_tenant_view("bob", shared, _state("bob"))

        a_sources = next(sv.sources for sv in view_a.stories if sv.id == "s1")
        b_sources = next(sv.sources for sv in view_b.stories if sv.id == "s1")

        assert a_sources == b_sources == ["ap", "reuters", "bbc"]

    def test_all_stories_present_for_all_tenants(self):
        """Every story in the projection appears in every tenant's view."""
        stories = [_story(f"s{i}") for i in range(5)]
        shared = _projection(*stories)

        view_a = build_tenant_view("alice", shared, _state("alice"))
        view_b = build_tenant_view("bob", shared, _state("bob"))

        a_ids = {sv.id for sv in view_a.stories}
        b_ids = {sv.id for sv in view_b.stories}
        expected = {f"s{i}" for i in range(5)}

        assert a_ids == expected
        assert b_ids == expected

    def test_confidence_immutable_across_tenants(self):
        """Personal annotations must not alter the confidence value in the view."""
        shared = _projection(_story("s1", confidence=0.92))
        state_a = _state("alice", pinned_ids=frozenset({"s1"}), seen_ids=frozenset({"s1"}))
        view_a = build_tenant_view("alice", shared, state_a)

        pinned_sv = next(sv for sv in view_a.stories if sv.id == "s1")
        # pinning / seeing must not touch confidence
        assert pinned_sv.confidence == 0.92
        assert pinned_sv.is_pinned is True
        assert pinned_sv.is_seen is True


# ---------------------------------------------------------------------------
# (C) CROSS-TENANT ACCESS: rejected at every boundary
# ---------------------------------------------------------------------------

class TestCrossTenantRejection:
    def test_assert_tenant_raises_on_mismatch(self):
        """_assert_tenant must raise TenantIsolationError for mismatched ids."""
        with pytest.raises(TenantIsolationError, match="alice"):
            _assert_tenant("alice", "bob", "test_context")

    def test_assert_tenant_passes_on_match(self):
        """_assert_tenant must not raise when ids match."""
        _assert_tenant("alice", "alice")  # no exception

    def test_build_tenant_view_rejects_wrong_state(self):
        """build_tenant_view must raise if state.tenant_id != tenant_id."""
        shared = _projection(_story("s1"))
        state_bob = _state("bob")

        with pytest.raises(TenantIsolationError):
            build_tenant_view("alice", shared, state_bob)

    def test_get_tenant_state_rejects_poisoned_store(self):
        """If the store contains a poisoned entry (wrong tenant_id), raise."""
        # Simulate a bug where alice's key maps to bob's state
        poisoned_states = {"alice": _state("bob")}

        with pytest.raises(TenantIsolationError):
            get_tenant_state("alice", poisoned_states)

    def test_merge_preference_rejects_wrong_tenant(self):
        state_bob = _state("bob")
        with pytest.raises(TenantIsolationError):
            merge_tenant_preference("alice", state_bob, "key", "value")

    def test_mark_seen_rejects_wrong_tenant(self):
        state_bob = _state("bob")
        with pytest.raises(TenantIsolationError):
            mark_seen("alice", state_bob, "s1")

    def test_pin_story_rejects_wrong_tenant(self):
        state_bob = _state("bob")
        with pytest.raises(TenantIsolationError):
            pin_story("alice", state_bob, "s1")

    def test_hide_story_rejects_wrong_tenant(self):
        state_bob = _state("bob")
        with pytest.raises(TenantIsolationError):
            hide_story("alice", state_bob, "s1")

    def test_add_comment_rejects_wrong_tenant(self):
        state_bob = _state("bob")
        with pytest.raises(TenantIsolationError):
            add_comment("alice", state_bob, "s1", "evil comment")

    def test_follow_topic_rejects_wrong_tenant(self):
        state_bob = _state("bob")
        with pytest.raises(TenantIsolationError):
            follow_topic("alice", state_bob, "politics")

    def test_isolation_error_message_contains_both_tenant_ids(self):
        """Error message must name both tenants so it's diagnosable in logs."""
        state_bob = _state("bob")
        with pytest.raises(TenantIsolationError, match="alice") as exc_info:
            build_tenant_view("alice", _projection(), state_bob)

        msg = str(exc_info.value)
        assert "alice" in msg
        assert "bob" in msg

    def test_tenant_view_tenant_id_matches(self):
        """Every StoryView in the TenantView must carry the correct tenant_id."""
        shared = _projection(_story("s1"), _story("s2"))
        view = build_tenant_view("alice", shared, _state("alice"))

        for sv in view.stories:
            assert sv.tenant_id == "alice"

    def test_tenant_view_owner_matches(self):
        """TenantView.tenant_id must equal the requested tenant_id."""
        view = build_tenant_view("alice", _projection(_story("x")), _state("alice"))
        assert view.tenant_id == "alice"


# ---------------------------------------------------------------------------
# Feed ordering and state mutation
# ---------------------------------------------------------------------------

class TestFeedOrdering:
    def test_pinned_stories_float_to_top(self):
        """Pinned stories must appear before unpinned ones regardless of confidence."""
        s_high = _story("high", confidence=0.95)
        s_low = _story("low", confidence=0.50)
        shared = _projection(s_high, s_low)  # ordered high → low by default
        # Pin the low-confidence story
        state = _state("alice", pinned_ids=frozenset({"low"}))
        view = build_tenant_view("alice", shared, state)
        vis = visible_stories(view)

        assert vis[0].id == "low"   # pinned floats up
        assert vis[1].id == "high"

    def test_hidden_stories_excluded_from_visible(self):
        """visible_stories() must not include hidden stories."""
        s1, s2, s3 = _story("a"), _story("b"), _story("c")
        shared = _projection(s1, s2, s3)
        state = _state("alice", hidden_ids=frozenset({"b"}))
        view = build_tenant_view("alice", shared, state)
        vis = visible_stories(view)

        ids = [sv.id for sv in vis]
        assert "b" not in ids
        assert "a" in ids
        assert "c" in ids

    def test_hidden_stories_included_when_requested(self):
        """visible_stories(include_hidden=True) returns all stories."""
        shared = _projection(_story("a"), _story("b"))
        state = _state("alice", hidden_ids=frozenset({"a"}))
        view = build_tenant_view("alice", shared, state)

        all_sv = visible_stories(view, include_hidden=True)
        assert len(all_sv) == 2

    def test_hidden_stories_appear_at_tail(self):
        """Hidden stories must be at the end of view.stories (tail), not dropped."""
        s1, s2 = _story("vis"), _story("hid")
        shared = _projection(s1, s2)
        state = _state("alice", hidden_ids=frozenset({"hid"}))
        view = build_tenant_view("alice", shared, state)

        assert view.stories[-1].id == "hid"
        assert view.stories[0].id == "vis"

    def test_stale_id_in_projection_skipped(self):
        """An ordered_ids entry not present in stories_by_id must be skipped."""
        s1 = _story("real")
        shared = SharedVeracityProjection(
            stories_by_id={"real": s1},
            ordered_ids=["ghost", "real"],  # "ghost" is stale
        )
        view = build_tenant_view("alice", shared, _state("alice"))
        ids = [sv.id for sv in view.stories]

        assert "ghost" not in ids
        assert "real" in ids

    def test_empty_projection_gives_empty_view(self):
        shared = _projection()
        view = build_tenant_view("alice", shared, _state("alice"))
        assert view.stories == []


# ---------------------------------------------------------------------------
# State mutation (pure / immutable)
# ---------------------------------------------------------------------------

class TestStateMutation:
    def test_mark_seen_adds_to_seen_ids(self):
        state = _state("alice")
        new_state = mark_seen("alice", state, "s1")
        assert "s1" in new_state.seen_ids
        assert "s1" not in state.seen_ids  # original unchanged

    def test_pin_story_adds_to_pinned_ids(self):
        state = _state("alice")
        new_state = pin_story("alice", state, "s2")
        assert "s2" in new_state.pinned_ids
        assert "s2" not in state.pinned_ids

    def test_hide_story_adds_to_hidden_ids(self):
        state = _state("alice")
        new_state = hide_story("alice", state, "s3")
        assert "s3" in new_state.hidden_ids
        assert "s3" not in state.hidden_ids

    def test_add_comment_sets_comment(self):
        state = _state("alice")
        new_state = add_comment("alice", state, "s4", "interesting!")
        assert new_state.comments["s4"] == "interesting!"
        assert "s4" not in state.comments

    def test_add_comment_overwrites_previous(self):
        state = add_comment("alice", _state("alice"), "s5", "first")
        new_state = add_comment("alice", state, "s5", "second")
        assert new_state.comments["s5"] == "second"

    def test_follow_topic_adds_topic(self):
        state = _state("alice")
        new_state = follow_topic("alice", state, "science")
        assert "science" in new_state.followed_topics
        assert "science" not in state.followed_topics

    def test_merge_preference_adds_key(self):
        state = _state("alice")
        new_state = merge_tenant_preference("alice", state, "view", "compact")
        assert new_state.preferences["view"] == "compact"
        assert "view" not in state.preferences

    def test_merge_preference_overrides_existing(self):
        state = merge_tenant_preference("alice", _state("alice"), "view", "full")
        new_state = merge_tenant_preference("alice", state, "view", "compact")
        assert new_state.preferences["view"] == "compact"

    def test_mutations_do_not_affect_other_tenant(self):
        """Mutating alice's state object must not affect bob's (immutability check)."""
        state_a = _state("alice")
        state_b = _state("bob")

        new_a = pin_story("alice", state_a, "shared_story")
        # bob's state is unchanged
        assert "shared_story" not in state_b.pinned_ids
        assert "shared_story" not in state_a.pinned_ids  # original also unchanged
        assert "shared_story" in new_a.pinned_ids


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_tenant_state_has_correct_id(self):
        state = empty_tenant_state("charlie")
        assert state.tenant_id == "charlie"
        assert len(state.pinned_ids) == 0
        assert len(state.seen_ids) == 0

    def test_single_story_single_tenant(self):
        shared = _projection(_story("only"))
        view = build_tenant_view("solo", shared, _state("solo"))
        assert len(view.stories) == 1
        assert view.stories[0].id == "only"
        assert view.stories[0].tenant_id == "solo"

    def test_many_tenants_same_projection(self):
        """Any number of tenants share the same projection; no allocation per tenant."""
        shared = _projection(_story("x"), _story("y"))
        tenants = [f"t{i}" for i in range(10)]
        views = [build_tenant_view(t, shared, _state(t)) for t in tenants]

        # All views reference the same shared projection
        proj_id = id(shared)
        for view in views:
            assert view.shared_projection_id == proj_id

        # All views contain both stories
        for view in views:
            assert {sv.id for sv in view.stories} == {"x", "y"}

    def test_tenant_id_default_cauri_is_valid(self):
        """The default tenant 'cauri' (from events.py) is a normal tenant in the model."""
        state = _state("cauri")
        shared = _projection(_story("news"))
        view = build_tenant_view("cauri", shared, state)
        assert view.tenant_id == "cauri"
        assert view.stories[0].tenant_id == "cauri"
