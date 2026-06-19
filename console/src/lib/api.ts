/**
 * Thin client for the console command/query API (#304).
 *
 * The browser always calls **same-origin** `/console/api/*`: in dev a Next rewrite
 * (`CONSOLE_API_ORIGIN`, see next.config.ts) proxies it to the FastAPI app; in prod Caddy
 * routes it there after the #292 cutover. So the cookie rides along and there's no CORS.
 */

import type {
  Acquisition,
  ClaimDetail,
  ClaimsResponse,
  CommandResult,
  ConfigResponse,
  Overview,
  PipelineHealth,
  PromptDetail,
  PromptsResponse,
  SourcesResponse,
  Spend,
  StoriesPage,
  StoryDetail,
} from "./types";

const BASE = "/console/api";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function detail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // not JSON — fall through
  }
  return res.statusText || `request failed (${res.status})`;
}

export async function apiGet<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { credentials: "include", signal });
  if (!res.ok) throw new ApiError(res.status, await detail(res));
  return (await res.json()) as T;
}

/**
 * Run an operator command. Every command is an audited `ADMIN_*` event — the only way the
 * console mutates state (D5/D28). `requires_signoff` commands should be confirmed in the UI
 * before this is called.
 */
export async function runCommand(
  name: string,
  body: Record<string, unknown>,
): Promise<CommandResult> {
  const res = await fetch(`${BASE}/commands/${name}`, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, await detail(res));
  return (await res.json()) as CommandResult;
}

// ── typed endpoint helpers ────────────────────────────────────────────────────────────────

export function getStories(
  params: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<StoriesPage> {
  const q = new URLSearchParams();
  if (params.limit != null) q.set("limit", String(params.limit));
  if (params.offset != null) q.set("offset", String(params.offset));
  const qs = q.toString();
  return apiGet<StoriesPage>(`/stories${qs ? `?${qs}` : ""}`, signal);
}

export function getStory(nodeId: string, signal?: AbortSignal): Promise<StoryDetail> {
  return apiGet<StoryDetail>(`/stories/${encodeURIComponent(nodeId)}`, signal);
}

export function getOverview(signal?: AbortSignal): Promise<Overview> {
  return apiGet<Overview>("/overview", signal);
}

export function getSources(signal?: AbortSignal): Promise<SourcesResponse> {
  return apiGet<SourcesResponse>("/sources", signal);
}

export function getClaims(
  params: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<ClaimsResponse> {
  const q = new URLSearchParams();
  if (params.limit != null) q.set("limit", String(params.limit));
  if (params.offset != null) q.set("offset", String(params.offset));
  const qs = q.toString();
  return apiGet<ClaimsResponse>(`/claims${qs ? `?${qs}` : ""}`, signal);
}

export function getClaim(claimId: string, signal?: AbortSignal): Promise<ClaimDetail> {
  return apiGet<ClaimDetail>(`/claims/${encodeURIComponent(claimId)}`, signal);
}

export function getPipeline(signal?: AbortSignal): Promise<PipelineHealth> {
  return apiGet<PipelineHealth>("/pipeline", signal);
}

export function getConfig(signal?: AbortSignal): Promise<ConfigResponse> {
  return apiGet<ConfigResponse>("/config", signal);
}

export function getPrompts(signal?: AbortSignal): Promise<PromptsResponse> {
  return apiGet<PromptsResponse>("/prompts", signal);
}

export function getPrompt(key: string, signal?: AbortSignal): Promise<PromptDetail> {
  return apiGet<PromptDetail>(`/prompts/${encodeURIComponent(key)}`, signal);
}

export function getSpend(signal?: AbortSignal): Promise<Spend> {
  return apiGet<Spend>("/spend", signal);
}

export function getAcquisition(signal?: AbortSignal): Promise<Acquisition> {
  return apiGet<Acquisition>("/acquisition", signal);
}
