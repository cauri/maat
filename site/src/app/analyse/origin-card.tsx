"use client";

import type { PublicOrigin } from "@/lib/types";

// The origin card (P16 #452/#453): who said it first, when, and who carries it now — the answer
// a claim-mode reader asked for, so it names names (locked with cauri). Copy discipline, also
// locked: this is "the earliest trace Maat FOUND", never "the origin" — deleted posts and private
// chats are unfindable, and the card must not overclaim. Provenance rides beside the verdict; it
// never explains or moves the score.

function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return `${n}`;
}

export default function OriginCard({ origin }: { origin: PublicOrigin }) {
  const empty =
    origin.confidence === "none" && !origin.earliest && !origin.social && origin.carriers === 0;
  return (
    <section className="origin">
      <h3 className="section">Where it came from</h3>
      <div className="card origin-card">
        {empty ? (
          <p className="origin-empty">
            No identifiable origin — none of the reporting Maat found credits a source for this
            claim.
          </p>
        ) : (
          <>
            {origin.attributed_to && (
              <div className="origin-row">
                <span className="origin-label">Attributed to</span>
                <span className="origin-value">{origin.attributed_to}</span>
              </div>
            )}
            {origin.chain.length > 1 && (
              <div className="origin-row">
                <span className="origin-label">Chain</span>
                <span className="origin-value origin-chain">{origin.chain.join(" → ")}</span>
              </div>
            )}
            {origin.earliest && (
              <div className="origin-row">
                <span className="origin-label">Earliest trace</span>
                <span className="origin-value">
                  <a href={origin.earliest.url} target="_blank" rel="noopener noreferrer">
                    {origin.earliest.source}
                  </a>
                  {" · "}
                  {origin.earliest.date}
                </span>
              </div>
            )}
            {origin.social && (
              <div className="origin-row">
                <span className="origin-label">Circulating</span>
                <span className="origin-value">
                  on {origin.social.platform} since {origin.social.date} —{" "}
                  <a href={origin.social.url} target="_blank" rel="noopener noreferrer">
                    {origin.social.author}
                  </a>
                  {origin.social.engagement > 0 && (
                    <span className="origin-engagement">
                      {" "}
                      · ≈{compact(origin.social.engagement)} interactions
                    </span>
                  )}
                </span>
              </div>
            )}
            {origin.carriers > 0 && (
              <div className="origin-row">
                <span className="origin-label">Carried by</span>
                <span className="origin-value">
                  {origin.carriers} independent outlet{origin.carriers === 1 ? "" : "s"}
                  {origin.top_carriers.length > 0 && <> — {origin.top_carriers.join(", ")}</>}
                </span>
              </div>
            )}
          </>
        )}
        <p className="origin-note">
          The earliest trace Maat could find — not necessarily where the claim truly began.
        </p>
      </div>
    </section>
  );
}
