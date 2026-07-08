import type { Analysis } from "./types";

// Server-side reads of a completed analysis, in-cluster. The card route and the page's OG
// metadata both need the payload before the browser is involved, so they hit the reader directly
// (SITE_API_INTERNAL, e.g. http://reader:8000) rather than going back out through Caddy.
export const INTERNAL_API =
  process.env.SITE_API_INTERNAL || process.env.SITE_API_ORIGIN || "http://localhost:8000";

export const PUBLIC_ORIGIN = process.env.SITE_PUBLIC_ORIGIN || "https://maat.press";

export async function loadAnalysis(id: string): Promise<Analysis | null> {
  if (!id) return null;
  try {
    const r = await fetch(`${INTERNAL_API}/api/v2/analyse/${encodeURIComponent(id)}`, {
      cache: "no-store",
    });
    if (!r.ok) return null;
    const d = await r.json();
    return (d?.analysis as Analysis) ?? null;
  } catch {
    return null;
  }
}
