import "server-only";

import { tool } from "ai";
import { z } from "zod";

import { siaApiOrigin } from "./sia";

/**
 * Sia's tools ARE the command/query API (#304), so she runs the exact audited path a human does.
 *
 * - **Read tools** execute server-side (fetch the projections) — safe, so they run automatically.
 * - **`propose_command`** has NO `execute`: it becomes a client tool the operator must confirm in
 *   the dock before anything is written (propose-and-confirm, D28). On confirm the dock runs the
 *   command and returns the result to Sia.
 *
 * Tools are built per-request so each carries the operator's session cookie to the gated API.
 */

export const SIA_COMMANDS = [
  "claim.correct",
  "claim.flag_laundering",
  "cluster.split",
  "cluster.merge",
  "claim.move",
  "config.set",
  "config.promote",
  "source.flag",
  "source.group",
  "clock.set",
  "prompt.update",
  "prompt.reviewed",
  "run.trigger",
] as const;

const COMMAND_REFERENCE = `Available commands and their args:
- claim.correct {claim_id, kind?, voice?, speaker?} — fix a claim's classification
- claim.flag_laundering {claim_id, abuse} — flag laundering/abuse the classifier missed
- cluster.split {cluster_id, into:[ids]} — split an over-merged cluster
- cluster.merge {merged:[cluster_ids], new_id?} — merge clusters that are one fact
- claim.move {claim_id, from_cluster, to_cluster} — move a claim between clusters
- config.set {key, value} — propose a knob change (recorded, not yet live)
- config.promote {key, value} — promote a knob to live (SIGN-OFF)
- source.flag {source, status:"allow"|"deny"} — allow or deny a source
- source.group {source, group} — group a source under a shared owner/wire
- clock.set {clock, paused:boolean} — pause/resume a pipeline clock
- prompt.update {key, text} — publish a new agent-prompt version (SIGN-OFF)
- prompt.reviewed {key} — mark a draft prompt reviewed
- run.trigger {stage?} — kick a pipeline run`;

function apiGet(path: string, cookie: string): Promise<unknown> {
  return fetch(`${siaApiOrigin()}${path}`, {
    headers: cookie ? { cookie } : {},
    cache: "no-store",
  }).then((res) =>
    res.ok ? res.json() : { error: `request failed (${res.status})` },
  );
}

export function buildSiaTools(cookie: string) {
  return {
    get_overview: tool({
      description: "KPIs: corpus counts, paused clocks, dead-letter count, last ingest.",
      inputSchema: z.object({}),
      execute: () => apiGet("/console/api/overview", cookie),
    }),
    get_stories: tool({
      description: "List stories with their plain-language credibility read (the product surface).",
      inputSchema: z.object({ limit: z.number().int().min(1).max(200).optional() }),
      execute: ({ limit }) => apiGet(`/console/api/stories?limit=${limit ?? 100}`, cookie),
    }),
    get_story: tool({
      description: "One story's full derivation: facts, forecasts, sources, trajectory.",
      inputSchema: z.object({ node_id: z.string() }),
      execute: ({ node_id }) => apiGet(`/console/api/stories/${encodeURIComponent(node_id)}`, cookie),
    }),
    get_sources: tool({
      description: "Outlets with their one canonical reliability number, lifecycle state, allow/deny.",
      inputSchema: z.object({}),
      execute: () => apiGet("/console/api/sources", cookie),
    }),
    get_claims: tool({
      description: "The claim inspector — recent claims with kind/voice/source.",
      inputSchema: z.object({ limit: z.number().int().min(1).max(200).optional() }),
      execute: ({ limit }) => apiGet(`/console/api/claims?limit=${limit ?? 100}`, cookie),
    }),
    get_pipeline: tool({
      description: "Pipeline health: stage activity, dead letters, throughput, calibration, alerts.",
      inputSchema: z.object({}),
      execute: () => apiGet("/console/api/pipeline", cookie),
    }),
    get_config: tool({
      description: "Tuning knobs with their default, active (live) and proposed values.",
      inputSchema: z.object({}),
      execute: () => apiGet("/console/api/config", cookie),
    }),
    get_prompts: tool({
      description: "The agent prompts, their status, and which need review.",
      inputSchema: z.object({}),
      execute: () => apiGet("/console/api/prompts", cookie),
    }),
    get_audit: tool({
      description: "Recent operator/Sia actions from the audit log.",
      inputSchema: z.object({ limit: z.number().int().min(1).max(200).optional() }),
      execute: ({ limit }) => apiGet(`/console/api/audit?limit=${limit ?? 50}`, cookie),
    }),
    propose_command: tool({
      description:
        "Stage an operator command for the human to confirm. NEVER executes on its own — the " +
        "operator reviews and applies it. Use this for every change to state.\n\n" +
        COMMAND_REFERENCE,
      inputSchema: z.object({
        command: z.enum(SIA_COMMANDS),
        args: z.record(z.string(), z.unknown()),
        rationale: z.string().describe("Why this change, and its expected effect."),
      }),
      // no execute → resolved by the dock after the operator confirms (human-in-the-loop)
    }),
  };
}
