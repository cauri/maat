// The public Analyse payload (serving/analyse.py :: public_payload) — verdicts only, no mechanism.

// The origin trace (P16 #452/#453) — claim mode only. Public BY DESIGN: provenance is the answer
// the reader asked for, so it names the earliest carrier; the copy must still say "earliest trace
// we FOUND", never "the origin". `social` authors arrive pre-anonymised unless plainly public.
export type PublicOrigin = {
  earliest: { date: string; source: string; url: string } | null;
  attributed_to: string;
  kind: string;
  chain: string[];
  carriers: number;
  top_carriers: string[];
  confidence: "strong" | "weak" | "none";
  social: { platform: string; author: string; url: string; date: string; engagement: number } | null;
};

export type PublicClaim = {
  text: string;
  voice: "own" | "attributed";
  speaker: string | null;
  central: boolean;
  extremity: string;
  // null when `checked` is false: an over-cap claim we never searched (#397) — no score, because a
  // number would imply we weighed it. The reader can force a check to fill it in.
  score: number | null;
  verdict: string;
  tier: "hi" | "mid" | "lo" | "floor" | "none" | "unchecked";
  checked: boolean;
  origin?: PublicOrigin | null; // claim mode only (#452)
};

export type PublicProjection = { text: string; speaker: string | null; verdict: string };

export type ShareCopy = {
  headline: string;
  tally: { total: number; corroborated: number; single_source: number; disputed: number; primary: number };
  og_title: string;
  og_description: string;
  twitter_text: string;
  linkedin_text: string;
  instagram_caption: string;
};

// One payload type, two kinds (P16 #450): an ARTICLE analysis carries url/source/title/publisher;
// a CLAIM analysis carries `checked` (the canonical "We checked: …" line — never the reader's raw
// input) + `origin`, and no publisher (nobody is publishing — the reader asked).
export type Analysis = {
  analysis_id: string;
  kind?: "article" | "claim"; // absent on pre-P16 cached article payloads
  url?: string;
  source?: string;
  title?: string | null;
  language: string;
  date?: string | null;
  publisher?: { domain: string; rated: boolean; score: number | null; review_started?: boolean };
  checked?: { display: string; language: string };
  origin?: PublicOrigin | null;
  overall: {
    score: number;
    band: string;
    label: string;
    reasons: string[];
    capped: boolean;
    forecast_only: boolean;
    unchecked?: number; // claims skipped over the live cap, shown as "Not checked" (#397)
  };
  claims: PublicClaim[];
  projections: PublicProjection[];
  share: ShareCopy;
  scope: string;
  analysed_at?: string;
};
