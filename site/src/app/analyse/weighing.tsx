"use client";

// The "weighing" stage: a balance scale doing the work — the Ma'at weighing, thematically exact.
// The beam oscillates (CSS), and a live count tracks claims as they resolve.

export default function Weighing({ done, total }: { done: number; total: number }) {
  return (
    <div className="weigh-stage" aria-live="polite">
      <svg className="weigh-scale" viewBox="0 0 100 88" role="img" aria-label="Weighing the claims">
        <circle cx="50" cy="9" r="4" fill="var(--gold)" />
        <line x1="50" y1="9" x2="50" y2="68" stroke="var(--gold)" strokeWidth="2.5" />
        <path d="M36 68 L64 68 L68 76 L32 76 Z" fill="var(--gold)" />
        <g className="weigh-beam">
          <line x1="14" y1="22" x2="86" y2="22" stroke="var(--gold)" strokeWidth="2.5" strokeLinecap="round" />
          <line x1="14" y1="13" x2="14" y2="22" stroke="var(--gold)" strokeWidth="1.4" />
          <line x1="86" y1="13" x2="86" y2="22" stroke="var(--gold)" strokeWidth="1.4" />
          <path d="M4 40 A10 8 0 0 0 24 40 Z" fill="var(--gold-wash)" stroke="var(--gold)" strokeWidth="1.4" />
          <path d="M76 40 A10 8 0 0 0 96 40 Z" fill="var(--gold-wash)" stroke="var(--gold)" strokeWidth="1.4" />
          <line x1="14" y1="22" x2="14" y2="40" stroke="var(--gold)" strokeWidth="0.8" />
          <line x1="86" y1="22" x2="86" y2="40" stroke="var(--gold)" strokeWidth="0.8" />
        </g>
      </svg>
      <div className="weigh-count">
        weighing {Math.min(done, total)} of {total}…
      </div>
    </div>
  );
}
