// The public Analyse payload (serving/analyse.py :: public_payload) — verdicts only, no mechanism.

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

export type Analysis = {
  analysis_id: string;
  url: string;
  source: string;
  title: string | null;
  language: string;
  date: string | null;
  publisher: { domain: string; rated: boolean; score: number | null; review_started?: boolean };
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
