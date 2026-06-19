import { cn } from "@/lib/utils";

/**
 * The plain-language credibility read for the **product surfaces** (Stories, the app's own
 * language) — a worded label + a tone dot, never a raw score bar or percentage (D25/D26). The
 * numeric score is shown only in the engine-room "why" (derivation), where transparency is the point.
 */
function tone(score: number, forecastOnly: boolean): string {
  if (forecastOnly) return "bg-sky-500";
  if (score >= 67) return "bg-emerald-500";
  if (score >= 34) return "bg-amber-500";
  return "bg-rose-500";
}

export function ScoreBadge({
  label,
  score,
  forecastOnly,
  capped,
  className,
}: {
  label: string;
  score: number;
  forecastOnly: boolean;
  capped?: boolean;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
        className,
      )}
      title={capped ? "Capped — see the derivation" : undefined}
    >
      <span className={cn("size-2 shrink-0 rounded-full", tone(score, forecastOnly))} />
      <span className="truncate">{label}</span>
      {capped && <span className="text-muted-foreground">·&nbsp;capped</span>}
    </span>
  );
}
