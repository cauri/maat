/**
 * Live event-stream wiring.
 *
 * The console subscribes to the FastAPI command/query API's Server-Sent-Events
 * endpoint (#304) for live projection updates — the Audit drawer and the live
 * indicator both read it. The path is configurable so the foundation can ship ahead
 * of #304; until that endpoint exists the stream simply reports "offline" and backs
 * off, which is the correct foundational state.
 *
 * `NEXT_PUBLIC_*` because the EventSource is opened in the browser.
 */

/** Base URL of the command/query API. Empty ⇒ same origin (the cutover target). */
export const CONSOLE_API_BASE = process.env.NEXT_PUBLIC_CONSOLE_API ?? "";

/** SSE path on that API. Empty ⇒ the live stream is disabled (no connection attempts). */
export const CONSOLE_SSE_PATH = process.env.NEXT_PUBLIC_CONSOLE_SSE_PATH ?? "/console/api/events";

/** Full SSE URL, or `null` when disabled. */
export function consoleSseUrl(): string | null {
  if (!CONSOLE_SSE_PATH) return null;
  return `${CONSOLE_API_BASE}${CONSOLE_SSE_PATH}`;
}
