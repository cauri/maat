import { cn } from "@/lib/utils";

/**
 * A tiny trajectory sparkline — shape of a source's reliability over its history (#192/#309). No
 * axes or numbers: it's a trend cue on the product surface. Colour tracks the net direction.
 */
export function Sparkline({
  points,
  width = 64,
  height = 18,
  className,
}: {
  points: number[];
  width?: number;
  height?: number;
  className?: string;
}) {
  if (!points || points.length < 2) {
    return <span className="text-xs text-muted-foreground/60">—</span>;
  }
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const stepX = width / (points.length - 1);
  const coords = points
    .map((p, i) => `${(i * stepX).toFixed(1)},${(height - ((p - min) / span) * height).toFixed(1)}`)
    .join(" ");
  const rising = points[points.length - 1] >= points[0];

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className={cn(rising ? "text-emerald-500" : "text-rose-500", className)}
      role="img"
      aria-label={`reliability trend, ${rising ? "rising" : "falling"}`}
    >
      <polyline
        points={coords}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        strokeLinecap="round"
      />
    </svg>
  );
}
