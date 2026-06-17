"""Event envelope + publish helpers, matching the Rust kernel's contract (maat-kerneld).

Subjects are `maat.events.<type>`; the JSON payload is the EventEnvelope the kernel decodes
and appends to the log.
"""

from __future__ import annotations

import json
from typing import Any

SUBJECT_PREFIX = "maat.events"

# --- Admin / operator-console actions (P8) ---------------------------------------------
# Every operator mutation is a typed event on the same append-only log the agents write to
# (D5/D20): the console publishes these; maat-kerneld is the single writer that folds them
# into the projections. The event log is therefore the audit trail for free.
ADMIN_CLASSIFICATION_CORRECTED = "admin.classification.corrected"  # F3: fix kind/voice/speaker
ADMIN_LAUNDERING_FLAGGED = "admin.laundering.flagged"  # F3: §5.2 abuse the classifier missed
ADMIN_CLUSTER_SPLIT = "admin.cluster.split"  # F3: an over-merged cluster, pulled apart (#20)
ADMIN_CLUSTER_MERGED = "admin.cluster.merged"  # F3: distinct clusters that are one fact
ADMIN_CLAIM_MOVED = "admin.claim.moved"  # F3: a claim moved between clusters
ADMIN_THRESHOLD_CHANGED = "admin.threshold.changed"  # F5: a proposed config change
ADMIN_RUN_TRIGGERED = "admin.run.triggered"  # F4: operator kicked a pipeline stage
ADMIN_SOURCE_FLAGGED = "admin.source.flagged"  # A2: allow / deny a source
ADMIN_SOURCE_GROUPED = "admin.source.grouped"  # A2: ownership / wire / copy-network grouping
ADMIN_CLOCK_SET = "admin.clock.set"  # A1: pause / resume a clock (the next tick reads the flag)
ADMIN_PROMPT_UPDATED = "admin.prompt.updated"  # P8: a new active version of an agent prompt
# P8/#189: operator cleared the "needs review" tag on a draft-seed prompt. Informational ONLY —
# prompts are already live; this is just a review marker, fully decoupled from whether any path
# runs. Read at runtime like admin.clock.set: ``prompts.needs_review`` checks for this event.
ADMIN_PROMPT_REVIEWED = "admin.prompt.reviewed"  # the operator marked a draft prompt as reviewed
ADMIN_CONFIG_PROMOTED = "admin.config.promoted"  # P8/#184: promote a proposed threshold to live
# Admin-console login audit (#163; D31). The console publishes these best-effort so the audit
# log records "who signed in, when"; auth itself is a stateless signed cookie (serving/admin_auth.py)
# that does not depend on the bus — a publish failure never blocks (or grants) a login.
ADMIN_SESSION_CREATED = "admin.session.created"  # an operator signed in (Google sub + email)
ADMIN_SESSION_REVOKED = "admin.session.revoked"  # an operator signed out

ADMIN_EVENT_TYPES = frozenset(
    {
        ADMIN_CLASSIFICATION_CORRECTED,
        ADMIN_LAUNDERING_FLAGGED,
        ADMIN_CLUSTER_SPLIT,
        ADMIN_CLUSTER_MERGED,
        ADMIN_CLAIM_MOVED,
        ADMIN_THRESHOLD_CHANGED,
        ADMIN_RUN_TRIGGERED,
        ADMIN_SOURCE_FLAGGED,
        ADMIN_SOURCE_GROUPED,
        ADMIN_CLOCK_SET,
        ADMIN_PROMPT_UPDATED,
        ADMIN_PROMPT_REVIEWED,
        ADMIN_CONFIG_PROMOTED,
        ADMIN_SESSION_CREATED,
        ADMIN_SESSION_REVOKED,
    }
)


# --- Pipeline / learning projections -----------------------------------------------------
# Point-in-time snapshot of a cluster's corroboration state, emitted by the projection-harvester
# (scripts/harvest.py, #39) so the §8 truth-over-time / calibration loop can fold a trajectory
# despite the kernel updating `clusters` in place. maat-kerneld folds it into cluster_snapshots,
# idempotent per (cluster_id, calendar-day).
CLUSTER_SNAPSHOT = "cluster.snapshot"

# Primary-source grounding (#228, §5/§8): the grounding agent (agents.grounding_agent) judges
# whether a cluster's fact is SUPPORTED / CONTRADICTED / NOT_ADDRESSED by its primary source, and
# emits this with the grounding-refined confidence. maat-kerneld updates the cluster row (grounding
# + confidence); the harvester carries the verdict into cluster_snapshots so it rides the trajectory
# (contradicted → REFUTED in resolve_outcome). stream_id IS the cluster_id.
CLUSTER_GROUNDED = "cluster.grounded"

# Automated contradiction detection (#229, §5/§8): the contradiction agent runs NLI between a claim
# and its nearest neighbours and emits one CLAIM_RELATED per detected relation (contradicts /
# entails) with the model's confidence score. maat-kerneld folds these into the claim_relations
# projection (the story graph #42 later folds the SAME events into typed edges). A high-confidence
# contradiction from a grounded / higher-confidence cluster feeds the refutation path. stream_id is
# a stable hash of the unordered claim pair.
CLAIM_RELATED = "claim.related"

# A claim refuted by a STRONGER, contradicting claim (#229) — the contradiction agent arbitrates by
# the two claims' cluster grounding / confidence and flags the loser. maat-kerneld sets claims.disputed,
# which the harvester folds into the cluster's `corrected` exactly like an operator correction, so the
# fact resolves REFUTED over time through the path #227 already built. stream_id IS the claim id.
CLAIM_DISPUTED = "claim.disputed"

# Automatic source-ownership resolution (#41 / #254, integrity): the ownership agent resolves a new
# source to its Wikidata entity and emits its direct controlling owners. corroborate folds these into
# the same `ownership` map the manual `admin.source.grouped` feeds (manual overrides), so co-owned
# outlets collapse to one independent originator without the operator typing each group. Conservative
# + operator-overridable — a wrong merge would HIDE real corroboration. stream_id is the source.
SOURCE_OWNERSHIP_RESOLVED = "source.ownership.resolved"

# Whole story-graph rebuild (#42/#43/#44, P4): the builder folds clusters into event-nodes +
# typed edges (develops/spawns/merges) + claim↔node links and emits the full graph in ONE event;
# maat-kerneld projects it atomically into story_nodes / story_edges / story_node_clusters /
# claim_node_links so the feed can return THREADED stories.
STORY_GRAPH_REBUILT = "story.graph.rebuilt"

# Inferred primary country for a story (#189, P6): the curation geo-tagger (agents.geotag_agent)
# fills the de-US re-ranker's gaps for clusters the TLD/language heuristic can't place, one event
# per cluster. The feed folds the latest per cluster at read time (no kernel projection needed) —
# it's a curation/ordering hint, never a veracity signal. stream_id IS the cluster_id.
STORY_GEO_INFERRED = "story.geo_inferred"

# --- Acquisition funnel (marketing site → operator console) -----------------------------
# The public marketing site (maat.press) publishes these as it records the visitor funnel
# (D5/D20): a page view, a "Download on the App Store" tap (which shows "coming soon"), and
# an optional launch-notify email. maat-kerneld folds them into the acquisition_signals /
# acquisition_signups projections; the console reads them on /acquisition. These are pre-user
# (anonymous visitors), so they carry the reserved tenant_id below rather than a real tenant.
PUBLIC_TENANT = "public"

ACQUISITION_PAGE_VIEWED = "acquisition.page_viewed"  # a visit to the landing page
ACQUISITION_CTA_CLICKED = "acquisition.cta_clicked"  # "Download on the App Store" tap
ACQUISITION_NOTIFY_REQUESTED = "acquisition.notify_requested"  # email left for launch

ACQUISITION_EVENT_TYPES = frozenset(
    {
        ACQUISITION_PAGE_VIEWED,
        ACQUISITION_CTA_CLICKED,
        ACQUISITION_NOTIFY_REQUESTED,
    }
)


def admin_event(
    target: str, *, actor: str = "operator", reason: str = "", **fields: Any
) -> dict[str, Any]:
    """Build the data payload for an admin action event.

    `target` is the primary subject (a claim or cluster id, also used as the event stream_id);
    `actor`/`reason` make the audit line answerable ("who, why"); `fields` carry the change
    itself (e.g. kind=, voice=, abuse=, into=). Pure — the caller publishes it.
    """
    return {"target": target, "actor": actor, "reason": reason, **fields}


def envelope(stream_id: str, type_: str, data: dict[str, Any], tenant_id: str = "cauri") -> bytes:
    return json.dumps(
        {"stream_id": stream_id, "type": type_, "data": data, "tenant_id": tenant_id}
    ).encode()


async def publish(
    nc: Any, type_: str, stream_id: str, data: dict[str, Any], tenant_id: str = "cauri"
) -> None:
    await nc.publish(f"{SUBJECT_PREFIX}.{type_}", envelope(stream_id, type_, data, tenant_id))
