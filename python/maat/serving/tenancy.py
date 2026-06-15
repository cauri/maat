"""Tenancy model (P5, issue #46): shared truth vs per-tenant/per-user state.

ARCHITECTURE PRINCIPLE (cauri)
================================
Truth is shared and singular — computed once from the event log by the veracity
core (corroboration, reputation, calibration). It does NOT diverge or get
duplicated per tenant. Every tenant sees the *same* confidence scores, labels,
and originator counts; these reflect facts about the world, not editorial
preferences.

Only the *personal layer* is partitioned: which topics a user follows, which
stories they have pinned or dismissed, curation preferences, and any social
annotations. This layer is keyed by tenant_id and is strictly isolated — tenant
A can never read or influence tenant B's personal state.

DATA MODEL
===========
- SharedVeracityProjection   — facts as known to the world, computed once
- TenantState                — per-user mutable preferences and seen-state
- TenantView                 — the assembled feed slice for a single tenant
                               (read-only; built by `build_tenant_view`)

PUBLIC API
===========
- build_tenant_view(tenant_id, shared, tenant_state)  → TenantView
- get_tenant_state(tenant_id, all_states)             → TenantState  (isolation guard)
- merge_tenant_preference(tenant_id, state, key, val) → TenantState
- visible_stories(view, *, seen=False)                → list[StoryView]

ISOLATION INVARIANT
====================
Every function that accepts a ``tenant_id`` parameter enforces that the result
is bound to that tenant. `get_tenant_state` raises `TenantIsolationError` if
asked to return a different tenant's state object. The `TenantView` dataclass
carries the `tenant_id` so callers can assert after composition.

Pure functions — no DB, no I/O, no async. Feed from projections.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


# ---------------------------------------------------------------------------
# Isolation guard
# ---------------------------------------------------------------------------


class TenantIsolationError(Exception):
    """Raised when a cross-tenant access attempt is detected.

    This is a programming-error exception (not a user-facing one). It signals
    that calling code tried to read tenant B's state while acting as tenant A.
    Catch it only at the boundary where you can log and surface the violation;
    do NOT swallow it silently.
    """


def _assert_tenant(expected: str, actual: str, context: str = "") -> None:
    """Raise TenantIsolationError if `actual` != `expected`.

    Called at every point where per-tenant state is accessed to ensure the data
    belongs to the requesting tenant, not a different one.
    """
    if expected != actual:
        detail = f" ({context})" if context else ""
        raise TenantIsolationError(
            f"Isolation violation{detail}: requested tenant '{expected}' but state "
            f"belongs to '{actual}'. Cross-tenant reads are forbidden."
        )


# ---------------------------------------------------------------------------
# Shared veracity projection (truth layer — not duplicated per tenant)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SharedStory:
    """A story's veracity-scored representation — identical for all tenants.

    These fields are computed by the veracity core (corroborate, calibrate,
    reputation agents) and are immutable here. No tenant can alter them.
    """

    id: str
    headline: str
    confidence: float          # 0.0–1.0 from corroboration
    label: str                 # "unverified" / "corroborated" / "well corroborated"
    independent_originators: int
    has_primary: bool
    extremity: str             # "routine" / "ordinary" / "notable" / "significant" / "extraordinary"
    sources: list[str] = field(default_factory=list)
    country: str = ""
    language: str = "en"


@dataclass(frozen=True)
class SharedVeracityProjection:
    """The complete shared truth layer.

    Built once from the event log and read by all tenants. Never cloned,
    never forked per tenant — tenants receive *views* over it, not copies.

    `stories_by_id` is the canonical index; `ordered_ids` is the default
    veracity-sorted ordering (descending confidence) used as the base feed.
    """

    stories_by_id: dict[str, SharedStory] = field(default_factory=dict)
    ordered_ids: list[str] = field(default_factory=list)
    source_reputations: dict[str, float] = field(default_factory=dict)   # source → 0–1
    calibration_snapshot: dict[str, Any] = field(default_factory=dict)  # opaque metadata


# ---------------------------------------------------------------------------
# Per-tenant / per-user state (personal layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TenantState:
    """All mutable personal state for one tenant.

    Strictly keyed by `tenant_id`. Any function that reads this struct must
    first call `_assert_tenant(caller_tenant_id, state.tenant_id)`.

    Fields
    -------
    tenant_id       — primary partition key; guards all access
    followed_topics — set of topic labels the user wants highlighted
    pinned_ids      — set of story ids the user has bookmarked
    seen_ids        — set of story ids the user has marked as read
    hidden_ids      — set of story ids the user explicitly dismissed
    preferences     — arbitrary key→value curation prefs (feed density, etc.)
    comments        — story_id → comment text (private notes, not shared)
    """

    tenant_id: str
    followed_topics: frozenset[str] = field(default_factory=frozenset)
    pinned_ids: frozenset[str] = field(default_factory=frozenset)
    seen_ids: frozenset[str] = field(default_factory=frozenset)
    hidden_ids: frozenset[str] = field(default_factory=frozenset)
    preferences: dict[str, Any] = field(default_factory=dict)
    comments: dict[str, str] = field(default_factory=dict)


def empty_tenant_state(tenant_id: str) -> TenantState:
    """Create a blank TenantState for a new tenant. Pure."""
    return TenantState(tenant_id=tenant_id)


# ---------------------------------------------------------------------------
# Assembled per-tenant view (read-only output)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoryView:
    """A single story as presented to one specific tenant.

    Combines the immutable shared truth with that tenant's personal annotations.
    The veracity fields (confidence, label, etc.) are copied from SharedStory
    and must not be modified — they are read-only facts about the world.
    """

    # --- shared truth (copied, not mutable) ---
    id: str
    headline: str
    confidence: float
    label: str
    independent_originators: int
    has_primary: bool
    extremity: str
    sources: list[str]
    country: str
    language: str

    # --- per-tenant personal layer ---
    tenant_id: str
    is_pinned: bool = False
    is_seen: bool = False
    is_hidden: bool = False
    personal_comment: str = ""


@dataclass(frozen=True)
class TenantView:
    """The complete assembled feed for one tenant at one moment in time.

    Invariant: every StoryView in `stories` has `story.tenant_id == tenant_id`.
    `build_tenant_view` enforces this; callers can assert it.
    """

    tenant_id: str
    stories: list[StoryView] = field(default_factory=list)
    # Carry through the shared projection reference so callers can verify it was
    # not diverged (same object identity across tenants).
    shared_projection_id: int = 0   # id() of the SharedVeracityProjection used


# ---------------------------------------------------------------------------
# Core composition function
# ---------------------------------------------------------------------------


def build_tenant_view(
    tenant_id: str,
    shared: SharedVeracityProjection,
    state: TenantState,
) -> TenantView:
    """Assemble the per-tenant feed view from shared truth + personal state.

    This is the key composition point: the shared projection is never modified.
    We produce a TenantView that layers personal annotations on top of the
    immutable veracity layer.

    Isolation guard: asserts that `state` belongs to `tenant_id` before reading
    any personal field, preventing cross-tenant data bleed.

    Story ordering: follows `shared.ordered_ids` (veracity-sorted by confidence).
    Hidden stories are moved to the tail of the list rather than dropped, so the
    caller can decide whether to surface them (e.g., for an "all stories" view).

    Pure — no side effects.
    """
    _assert_tenant(tenant_id, state.tenant_id, "build_tenant_view")

    visible: list[StoryView] = []
    hidden: list[StoryView] = []

    for sid in shared.ordered_ids:
        story = shared.stories_by_id.get(sid)
        if story is None:
            continue  # stale id; projection may have been pruned

        sv = StoryView(
            # --- shared truth (read-only) ---
            id=story.id,
            headline=story.headline,
            confidence=story.confidence,
            label=story.label,
            independent_originators=story.independent_originators,
            has_primary=story.has_primary,
            extremity=story.extremity,
            sources=list(story.sources),
            country=story.country,
            language=story.language,
            # --- personal layer ---
            tenant_id=tenant_id,
            is_pinned=(story.id in state.pinned_ids),
            is_seen=(story.id in state.seen_ids),
            is_hidden=(story.id in state.hidden_ids),
            personal_comment=state.comments.get(story.id, ""),
        )

        if sv.is_hidden:
            hidden.append(sv)
        else:
            visible.append(sv)

    # Pinned stories float to the very top of the visible list.
    pinned = [sv for sv in visible if sv.is_pinned]
    unpinned = [sv for sv in visible if not sv.is_pinned]
    ordered = pinned + unpinned + hidden

    return TenantView(
        tenant_id=tenant_id,
        stories=ordered,
        shared_projection_id=id(shared),
    )


# ---------------------------------------------------------------------------
# State accessors with isolation enforcement
# ---------------------------------------------------------------------------


def get_tenant_state(
    tenant_id: str,
    all_states: dict[str, TenantState],
) -> TenantState:
    """Return the TenantState for `tenant_id` from the store.

    Raises TenantIsolationError if the returned state's tenant_id does not match
    `tenant_id` (guards against keying bugs where the wrong state slips in).

    Returns an empty TenantState if `tenant_id` is not present — no cross-tenant
    default is ever used.
    """
    state = all_states.get(tenant_id)
    if state is None:
        return empty_tenant_state(tenant_id)
    _assert_tenant(tenant_id, state.tenant_id, "get_tenant_state")
    return state


def merge_tenant_preference(
    tenant_id: str,
    state: TenantState,
    key: str,
    value: Any,
) -> TenantState:
    """Return a new TenantState with `preferences[key] = value`.

    Isolation guard: asserts state belongs to tenant_id before mutating.
    Pure (returns a new frozen dataclass instance via `replace`).
    """
    _assert_tenant(tenant_id, state.tenant_id, "merge_tenant_preference")
    new_prefs = {**state.preferences, key: value}
    return replace(state, preferences=new_prefs)


def mark_seen(tenant_id: str, state: TenantState, story_id: str) -> TenantState:
    """Return a new TenantState with `story_id` added to seen_ids. Pure."""
    _assert_tenant(tenant_id, state.tenant_id, "mark_seen")
    return replace(state, seen_ids=state.seen_ids | {story_id})


def pin_story(tenant_id: str, state: TenantState, story_id: str) -> TenantState:
    """Return a new TenantState with `story_id` added to pinned_ids. Pure."""
    _assert_tenant(tenant_id, state.tenant_id, "pin_story")
    return replace(state, pinned_ids=state.pinned_ids | {story_id})


def hide_story(tenant_id: str, state: TenantState, story_id: str) -> TenantState:
    """Return a new TenantState with `story_id` added to hidden_ids. Pure."""
    _assert_tenant(tenant_id, state.tenant_id, "hide_story")
    return replace(state, hidden_ids=state.hidden_ids | {story_id})


def add_comment(
    tenant_id: str, state: TenantState, story_id: str, text: str
) -> TenantState:
    """Return a new TenantState with a private comment attached to `story_id`. Pure."""
    _assert_tenant(tenant_id, state.tenant_id, "add_comment")
    new_comments = {**state.comments, story_id: text}
    return replace(state, comments=new_comments)


def follow_topic(tenant_id: str, state: TenantState, topic: str) -> TenantState:
    """Return a new TenantState with `topic` added to followed_topics. Pure."""
    _assert_tenant(tenant_id, state.tenant_id, "follow_topic")
    return replace(state, followed_topics=state.followed_topics | {topic})


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


def visible_stories(view: TenantView, *, include_hidden: bool = False) -> list[StoryView]:
    """Return the non-hidden stories from a TenantView in feed order.

    If `include_hidden=True`, all stories (including hidden) are returned.
    """
    if include_hidden:
        return list(view.stories)
    return [sv for sv in view.stories if not sv.is_hidden]


def shared_truth_is_identical(
    view_a: TenantView,
    view_b: TenantView,
    shared: SharedVeracityProjection,
) -> bool:
    """Assert that both views were built from the same shared projection.

    Returns True if both views reference the same SharedVeracityProjection
    object (same `id()`). This is the runtime check that truth has not been
    forked or diverged per tenant.
    """
    expected = id(shared)
    return view_a.shared_projection_id == expected and view_b.shared_projection_id == expected
