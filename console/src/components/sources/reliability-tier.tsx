import { cn } from "@/lib/utils";

/**
 * The ONE canonical reliability read, as a plain-language tier — the product surface shows words,
 * not a raw score bar (D25/D26). The underlying [0,1] number is operator-only (workspace "why").
 */
export function reliabilityTier(reliability: number | null): { label: string; tone: string } {
  if (reliability == null) return { label: "Unrated", tone: "bg-muted-foreground/40" };
  if (reliability >= 0.7) return { label: "Reliable", tone: "bg-emerald-500" };
  if (reliability >= 0.45) return { label: "Mixed", tone: "bg-amber-500" };
  return { label: "Low", tone: "bg-rose-500" };
}

export function ReliabilityTier({
  reliability,
  className,
}: {
  reliability: number | null;
  className?: string;
}) {
  const t = reliabilityTier(reliability);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
        className,
      )}
    >
      <span className={cn("size-2 shrink-0 rounded-full", t.tone)} />
      {t.label}
    </span>
  );
}
