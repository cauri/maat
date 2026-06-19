import "server-only";

/**
 * Server-side Sia config (#306). Sia's persona is a **managed prompt** in the Python registry
 * (`prompts.py` key `sia`), served by the command/query API and reviewable/editable in-console
 * (D29) — so the route fetches it at chat time rather than hardcoding it here. The model is
 * claude-opus-4-8 (cauri's call: the marquee collaborator, cost-no-object).
 */

// Server-side absolute origin for the FastAPI command/query API (the browser uses same-origin; a
// server fetch needs an absolute URL). On the box this is the loopback FastAPI; in dev it's the
// CONSOLE_API_ORIGIN proxy target.
const INTERNAL_API =
  process.env.CONSOLE_API_INTERNAL ?? process.env.CONSOLE_API_ORIGIN ?? "http://127.0.0.1:8000";

export const SIA_MODEL = "claude-opus-4-8";

export function siaApiOrigin(): string {
  return INTERNAL_API;
}

/**
 * Fetch Sia's persona from the prompt registry and fill the page context. Returns null if the
 * persona can't be reached — the route then refuses to run rather than inventing a prompt (the
 * persona is co-designed with cauri; it is never fabricated client-side).
 */
export async function getSiaSystemPrompt(
  room: string,
  selection: unknown,
  cookie: string,
): Promise<string | null> {
  try {
    const res = await fetch(`${INTERNAL_API}/console/api/prompts/sia`, {
      headers: cookie ? { cookie } : {},
      cache: "no-store",
    });
    if (!res.ok) return null;
    const body = (await res.json()) as { text?: string };
    if (!body.text) return null;
    const sel =
      selection && (typeof selection !== "object" || Object.keys(selection).length > 0)
        ? JSON.stringify(selection)
        : "Nothing is selected.";
    return body.text.replaceAll("{room}", room || "Unknown").replaceAll("{selection}", sel);
  } catch {
    return null;
  }
}
