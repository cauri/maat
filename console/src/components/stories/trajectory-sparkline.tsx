import type { TrajectoryPoint } from "@/lib/types";
import { cn } from "@/lib/utils";

/** A dependency-free credibility-over-time sparkline (#39). Scores are 0–100. */
export function TrajectorySparkline({
  points,
  className,
}: {
  points: TrajectoryPoint[];
  className?: string;
}) {
  if (points.length < 2) {
    return <span className="text-xs text-muted-foreground">Not enough history yet</span>;
  }
  const w = 220;
  const h = 44;
  const pad = 3;
  const n = points.length;
  const x = (i: number) => pad + (i * (w - 2 * pad)) / (n - 1);
  const y = (s: number) => h - pad - (Math.max(0, Math.min(100, s)) / 100) * (h - 2 * pad);
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.score).toFixed(1)}`).join(" ");
  const area = `${line} L${x(n - 1).toFixed(1)},${h - pad} L${x(0).toFixed(1)},${h - pad} Z`;
  const last = points[n - 1].score;
  const stroke =
    last >= 67 ? "text-emerald-500" : last >= 34 ? "text-amber-500" : "text-rose-500";

  return (
    <svg
      viewBox={`0 0 ${w} ${h}`}
      className={cn("h-11 w-full", stroke, className)}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Credibility trajectory, latest ${last} of 100`}
    >
      <path d={area} fill="currentColor" opacity={0.12} />
      <path d={line} fill="none" stroke="currentColor" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
      <circle cx={x(n - 1)} cy={y(last)} r={2.5} fill="currentColor" />
    </svg>
  );
}
