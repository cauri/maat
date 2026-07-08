"use client";

// The "reading" stage: a miniature, representative rendering of the article (P14, #365). Skeleton
// lines stand in for body text (Maat never republishes article prose); the claim highlights are
// placed at their TRUE vertical position (server-sent 0–1 fraction of where the evidence sits) and
// light up as claims are found. Purely decorative → aria-hidden; the real content is the list.

export type MiniFact = { text: string; central: boolean; position: number };

const LINE_WIDTHS = [100, 96, 92, 88, 99, 84, 94, 90, 97, 82, 95, 91, 88, 93, 86, 79];

export default function MiniPage({
  source,
  title,
  facts,
  leaving,
  registerHighlight,
}: {
  source: string;
  title: string | null;
  facts: MiniFact[];
  leaving: boolean;
  registerHighlight: (i: number, el: HTMLElement | null) => void;
}) {
  return (
    <div className={`minipage${leaving ? " leaving" : ""}`} aria-hidden="true">
      <div className="mp-source">{source || "article"}</div>
      <div className="mp-title">{title || "Reading the article…"}</div>
      <div className="mp-body" style={{ minHeight: 264 }}>
        {LINE_WIDTHS.map((w, i) => (
          <div key={i} className="mp-line" style={{ width: `${w}%` }} />
        ))}
        {facts.map((f, i) => (
          <div
            key={i}
            ref={(el) => registerHighlight(i, el)}
            className={`mp-hl lit${f.central ? " central" : ""}`}
            style={{ top: `${Math.min(0.95, Math.max(0, f.position)) * 100}%`, animationDelay: `${i * 85}ms` }}
          />
        ))}
        <div className="mp-scan" />
      </div>
      <div className="mp-count">
        <span className="feather-anim" aria-hidden="true">
          ⚖
        </span>
        {facts.length} claim{facts.length === 1 ? "" : "s"} found
      </div>
    </div>
  );
}
