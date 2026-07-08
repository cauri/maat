import { readFileSync } from "node:fs";
import { ImageResponse } from "next/og";

import { loadAnalysis } from "@/lib/server";
import type { Analysis } from "@/lib/types";

// The shareable card image (P14, #365): a server-rendered PNG digest of one analysis, in Maat's
// warm/editorial brand. Two shapes — landscape (1200×630) for link unfurls / Twitter cards, and
// square (1080×1080) for Instagram + download. Immutable per (id, format): cache hard.
//
// Reads the completed analysis from the reader in-cluster (SITE_API_INTERNAL). "What, not how":
// the card shows verdict, score, one ruling and a claim tally — never the mechanism.

export const runtime = "nodejs";
export const dynamic = "force-dynamic"; // keyed by ?id — never statically prerendered

// Fonts must be embedded for Satori. `new URL(..., import.meta.url)` is the reference Next's file
// tracer follows to include the TTF in the standalone bundle; readFileSync(URL) then loads it.
const SERIF = readFileSync(new URL("./fonts/PTSerif-Regular.ttf", import.meta.url));
const SERIF_BOLD = readFileSync(new URL("./fonts/PTSerif-Bold.ttf", import.meta.url));

const PAPER = "#f6f4ee";
const INK = "#16150f";
const MUT = "#6f6a5d";
const LINE = "#e6e0d3";
const GOLD = "#a8792e";

function bandColor(band: string): string {
  switch (band) {
    case "established":
    case "corroborated":
      return "#3b6d11";
    case "developing":
      return "#956211";
    case "thin":
    case "single":
      return "#b3572b";
    case "disqualified":
    case "disputed":
      return "#a02318";
    default:
      return MUT; // forecast / unknown
  }
}

function trim(s: string, n: number): string {
  const t = (s || "").replace(/\s+/g, " ").trim();
  return t.length <= n ? t : t.slice(0, n - 1).trimEnd() + "…";
}

function tallyFromClaims(claims: Analysis["claims"]): { total: number; corroborated: number; single_source: number; disputed: number } {
  const t = { total: claims.length, corroborated: 0, single_source: 0, disputed: 0 };
  for (const c of claims) {
    if (c.verdict.startsWith("Well corroborated") || c.verdict.startsWith("Corroborated")) t.corroborated++;
    else if (c.verdict.startsWith("Disputed")) t.disputed++;
    else if (c.verdict.startsWith("Only this source")) t.single_source++;
  }
  return t;
}

function tallyLine(a: Analysis): string {
  const t = a.share?.tally ?? tallyFromClaims(a.claims ?? []);
  const parts = [`${t.total} claim${t.total === 1 ? "" : "s"} weighed`];
  if (t.corroborated) parts.push(`${t.corroborated} corroborated`);
  if (t.disputed) parts.push(`${t.disputed} disputed`);
  if (t.single_source) parts.push(`${t.single_source} single-source`);
  return parts.join("  ·  ");
}

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const id = searchParams.get("id") || "";
  const square = searchParams.get("format") === "square";
  const W = square ? 1080 : 1200;
  const H = square ? 1080 : 630;
  const pad = square ? 84 : 72;

  const a = id ? await loadAnalysis(id) : null;

  const fonts = [
    { name: "PTSerif", data: SERIF, weight: 400 as const, style: "normal" as const },
    { name: "PTSerif", data: SERIF_BOLD, weight: 700 as const, style: "normal" as const },
  ];
  const headers = {
    "content-type": "image/png",
    // Immutable per (id, format) once analysed; a missing/pending analysis is only briefly cached.
    "cache-control": a ? "public, max-age=31536000, immutable" : "public, max-age=60",
  };

  const Wordmark = (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
      <div style={{ display: "flex", alignItems: "center", fontSize: square ? 40 : 34, fontWeight: 700 }}>
        <span style={{ color: GOLD, marginRight: 12 }}>⚖</span>Maat
      </div>
      <div style={{ display: "flex", color: MUT, fontSize: square ? 24 : 22 }}>maat.press/analyse</div>
    </div>
  );

  if (!a) {
    return new ImageResponse(
      (
        <div
          style={{
            width: "100%", height: "100%", display: "flex", flexDirection: "column",
            background: PAPER, color: INK, padding: pad, fontFamily: "PTSerif",
            justifyContent: "space-between",
          }}
        >
          {Wordmark}
          <div style={{ display: "flex", fontSize: square ? 60 : 52, fontWeight: 700, lineHeight: 1.15 }}>
            Weigh the news.
          </div>
          <div style={{ display: "flex", color: MUT, fontSize: square ? 30 : 26 }}>
            Paste any news article. Maat checks each claim against independent reporting.
          </div>
        </div>
      ),
      { width: W, height: H, fonts, headers },
    );
  }

  const color = bandColor(a.overall.band);
  const ruling = a.overall.reasons[0] ? trim(cap(a.overall.reasons[0]), 120) : "";
  const headline = a.overall.forecast_only ? a.overall.label : `${a.overall.label}`;

  return new ImageResponse(
    (
      <div
        style={{
          width: "100%", height: "100%", display: "flex", flexDirection: "column",
          background: PAPER, color: INK, padding: pad, fontFamily: "PTSerif",
          justifyContent: "space-between",
        }}
      >
        {Wordmark}

        {/* Article */}
        <div style={{ display: "flex", flexDirection: "column", marginTop: square ? 28 : 10 }}>
          <div style={{ display: "flex", fontSize: square ? 46 : 40, fontWeight: 700, lineHeight: 1.18 }}>
            {trim(a.title || "Untitled article", square ? 120 : 110)}
          </div>
          <div style={{ display: "flex", color: MUT, fontSize: square ? 28 : 24, marginTop: 14 }}>
            {a.source}
            {a.publisher.rated ? `  ·  publisher track record ${a.publisher.score}/100` : ""}
          </div>
        </div>

        {/* Verdict */}
        <div style={{ display: "flex", flexDirection: "column" }}>
          <div style={{ display: "flex", alignItems: "flex-end" }}>
            <div style={{ display: "flex", width: square ? 16 : 14, height: square ? 76 : 66, background: color, borderRadius: 4, marginRight: 22 }} />
            <div style={{ display: "flex", flexDirection: "column" }}>
              <div style={{ display: "flex", color, fontSize: square ? 66 : 58, fontWeight: 700, lineHeight: 1 }}>
                {headline}
              </div>
            </div>
            {!a.overall.forecast_only && (
              <div style={{ display: "flex", marginLeft: 22, color: MUT, fontSize: square ? 40 : 34, paddingBottom: 6 }}>
                {a.overall.score}/100
              </div>
            )}
          </div>
          {ruling && (
            <div style={{ display: "flex", fontSize: square ? 32 : 27, marginTop: 20, lineHeight: 1.3 }}>
              {ruling}
            </div>
          )}
          <div style={{ display: "flex", color: MUT, fontSize: square ? 26 : 22, marginTop: 16 }}>
            {tallyLine(a)}
          </div>
        </div>

        {/* Scope */}
        <div style={{ display: "flex", color: MUT, fontSize: square ? 22 : 19, borderTop: `1px solid ${LINE}`, paddingTop: 18, lineHeight: 1.35 }}>
          Maat weighs whether an article&apos;s factual claims hold up against independent reporting — not its tone or bias.
        </div>
      </div>
    ),
    { width: W, height: H, fonts, headers },
  );
}

function cap(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}
