/**
 * TypeScript mirror of the command/query API JSON contract (#304,
 * `python/maat/serving/console_api.py`). Kept in lockstep with `story_to_json` and the
 * command registry on the Python side.
 */

export interface StorySource {
  names: string[];
  reputation: number | null;
  wire: boolean;
}

export interface StoryFact {
  cluster_id: string;
  fact: string;
  fact_en: string | null;
  confidence: number;
  independent_originators: number;
  has_primary: boolean;
  extremity: string;
  grounding: string | null;
  disputed: boolean;
  is_headline: boolean;
  is_projection: boolean;
  sources: StorySource[];
}

export interface TrajectoryPoint {
  day: string;
  score: number;
  band: string;
}

/** A story as the list returns it (the credibility roll-up + counts). */
export interface Story {
  id: string;
  headline: string;
  headline_orig: string | null;
  score: number;
  band: string;
  label: string;
  forecast_only: boolean;
  capped: boolean;
  why: string;
  source_count: number;
  fact_count: number;
  forecast_count: number;
  cluster_count: number;
  first_seen: number;
  last_updated: number;
}

/** A story with its full transparent breakdown (the workspace view). */
export interface StoryDetail extends Story {
  facts: StoryFact[];
  forecasts: StoryFact[];
  trajectory: TrajectoryPoint[];
}

export interface StoriesPage {
  total: number;
  limit: number;
  offset: number;
  stories: Story[];
}

/** The Overview landing snapshot (#307) — counts, clock states, and pipeline freshness. */
export interface Overview {
  counts: { articles: number; claims: number; clusters: number; events: number };
  /** `true` = paused (mirrors `is_paused`); `false` = running. */
  clocks: { ingestion: boolean; extraction: boolean; corroboration: boolean };
  dead_letters: number;
  /** ISO timestamp of the last `article.ingested` event, or null if none yet. */
  last_ingest: string | null;
}

/** A source as the Sources room lists it — the ONE canonical reliability number + trajectory. */
export interface Source {
  source: string;
  articles: number;
  first_seen: string | null;
  last_seen: string | null;
  /** Canonical reliability in [0,1], or null when not yet rated (cold start). */
  reliability: number | null;
  /** Reliability sparkline over expanding history (oldest→newest), each in [0,1]. */
  trajectory: number[];
  /** Lifecycle: unregistered | registered | backfilling | scored | active. */
  state: string;
  /** Operator gate: allow (default) | deny. */
  status: "allow" | "deny";
}

export interface SourcesResponse {
  total: number;
  sources: Source[];
}

export interface CommandManifestEntry {
  name: string;
  event_type: string;
  summary: string;
  fields: string[];
  requires_signoff: boolean;
}

export interface CommandResult {
  ok: boolean;
  command: string;
  event_type: string;
  stream_id: string;
  requires_signoff: boolean;
}
