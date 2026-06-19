"use client";

import Link from "next/link";

import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CircleCheck,
  CirclePause,
  ListChecks,
  Network,
  Newspaper,
  ScrollText,
} from "lucide-react";

import { ScoreBadge } from "@/components/stories/score-badge";
import { useShell } from "@/components/shell/shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useOverview } from "@/hooks/use-overview";
import { useStories } from "@/hooks/use-stories";
import { isStale, relativeTime } from "@/lib/time";
import type { Story } from "@/lib/types";
import { cn } from "@/lib/utils";

const CLOCKS = [
  { key: "ingestion", label: "Ingestion" },
  { key: "extraction", label: "Extraction" },
  { key: "corroboration", label: "Corroboration" },
] as const;

function bandTone(score: number, forecastOnly: boolean): string {
  if (forecastOnly) return "bg-sky-500";
  if (score >= 67) return "bg-emerald-500";
  if (score >= 34) return "bg-amber-500";
  return "bg-rose-500";
}

/** "Needs you" = under-corroborated, capped, or forecast-only stories — the operator's queue. */
function attentionReason(story: Story): string | null {
  if (story.source_count <= 1) return "Single source";
  if (story.capped) return "Capped";
  if (story.forecast_only) return "Forecast only";
  return null;
}

export function OverviewDashboard() {
  const overview = useOverview();
  const stories = useStories();
  const { audit } = useShell();

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 sm:p-6">
      <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <KpiCard
          icon={Newspaper}
          label="Articles"
          value={overview.data?.counts.articles}
          loading={overview.isLoading}
          href="/pipeline"
          digest="Articles ingested into the pipeline. Open Pipeline for stage health →"
        />
        <KpiCard
          icon={ListChecks}
          label="Claims"
          value={overview.data?.counts.claims}
          loading={overview.isLoading}
          href="/claims"
          digest="Claims extracted from articles. Open the claim inspector →"
        />
        <KpiCard
          icon={Network}
          label="Clusters"
          value={overview.data?.counts.clusters}
          loading={overview.isLoading}
          href="/graph"
          digest="Corroborated fact clusters. Open the corroboration graph →"
        />
        <KpiCard
          icon={Activity}
          label="Events"
          value={overview.data?.counts.events}
          loading={overview.isLoading}
          onClick={audit.toggle}
          digest="Events on the append-only log. Open the audit log →"
        />
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <PipelineCard overview={overview.data} loading={overview.isLoading} error={overview.isError} />
        <StoriesSnapshotCard stories={stories.rows} total={stories.total} loading={stories.isLoading} />
      </section>

      <NeedsAttentionCard stories={stories.rows} loading={stories.isLoading} />
    </div>
  );
}

function KpiCard({
  icon: Icon,
  label,
  value,
  loading,
  digest,
  href,
  onClick,
}: {
  icon: typeof Newspaper;
  label: string;
  value: number | undefined;
  loading: boolean;
  digest: string;
  href?: string;
  onClick?: () => void;
}) {
  const inner = (
    <Card className="h-full transition-colors hover:border-ring/40 hover:bg-muted/40">
      <CardContent className="flex items-center justify-between gap-3 py-4">
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</span>
          {loading ? (
            <Skeleton className="h-8 w-16" />
          ) : (
            <span className="text-3xl font-semibold tabular-nums tracking-tight">
              {(value ?? 0).toLocaleString()}
            </span>
          )}
        </div>
        <Icon className="size-5 shrink-0 text-muted-foreground/60" />
      </CardContent>
    </Card>
  );

  const cls = "block rounded-xl text-left outline-none focus-visible:ring-2 focus-visible:ring-ring";
  const trigger = href ? (
    <Link href={href} className={cls}>
      {inner}
    </Link>
  ) : (
    <button type="button" onClick={onClick} className={cn(cls, "w-full")}>
      {inner}
    </button>
  );

  return (
    <Tooltip>
      <TooltipTrigger asChild>{trigger}</TooltipTrigger>
      <TooltipContent>{digest}</TooltipContent>
    </Tooltip>
  );
}

function PipelineCard({
  overview,
  loading,
  error,
}: {
  overview: ReturnType<typeof useOverview>["data"];
  loading: boolean;
  error: boolean;
}) {
  const lastIngest = overview?.last_ingest ? Date.parse(overview.last_ingest) : null;
  const stale = lastIngest != null && isStale(lastIngest, 6 * 60 * 60 * 1000);
  const dead = overview?.dead_letters ?? 0;

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-base">
          <Activity className="size-4" /> Pipeline
        </CardTitle>
        <Button asChild variant="ghost" size="sm">
          <Link href="/pipeline">
            Open <ArrowRight className="size-3.5" />
          </Link>
        </Button>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-4">
        {error ? (
          <p className="text-sm text-destructive">Couldn&apos;t load pipeline status.</p>
        ) : loading ? (
          <Skeleton className="h-28 w-full" />
        ) : (
          <>
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-sm text-muted-foreground">Last ingest</span>
              <span className={cn("text-sm font-medium", stale && "text-amber-600 dark:text-amber-500")}>
                {lastIngest ? relativeTime(lastIngest) : "never"}
                {stale && " · stale"}
              </span>
            </div>
            <div className="flex flex-col gap-2">
              {CLOCKS.map((c) => {
                const paused = overview?.clocks[c.key] ?? false;
                return (
                  <div key={c.key} className="flex items-center justify-between gap-2 text-sm">
                    <span className="text-muted-foreground">{c.label}</span>
                    <span
                      className={cn(
                        "inline-flex items-center gap-1.5 font-medium",
                        paused ? "text-muted-foreground" : "text-emerald-600 dark:text-emerald-500",
                      )}
                    >
                      {paused ? <CirclePause className="size-3.5" /> : <CircleCheck className="size-3.5" />}
                      {paused ? "Paused" : "Running"}
                    </span>
                  </div>
                );
              })}
            </div>
            <div className="mt-auto flex items-center justify-between gap-2 border-t pt-3 text-sm">
              <span className="text-muted-foreground">Dead letters</span>
              {dead > 0 ? (
                <Badge variant="destructive" className="gap-1">
                  <AlertTriangle className="size-3" /> {dead}
                </Badge>
              ) : (
                <span className="font-medium text-emerald-600 dark:text-emerald-500">0</span>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function StoriesSnapshotCard({
  stories,
  total,
  loading,
}: {
  stories: Story[] | undefined;
  total: number | undefined;
  loading: boolean;
}) {
  const bands = new Map<string, { count: number; score: number; forecast: boolean }>();
  for (const s of stories ?? []) {
    const cur = bands.get(s.band);
    if (cur) cur.count += 1;
    else bands.set(s.band, { count: 1, score: s.score, forecast: s.forecast_only });
  }
  const ordered = [...bands.entries()].sort((a, b) => b[1].score - a[1].score);
  const shown = stories?.length ?? 0;

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-base">
          <Newspaper className="size-4" /> Stories
        </CardTitle>
        <Button asChild variant="ghost" size="sm">
          <Link href="/stories">
            Open <ArrowRight className="size-3.5" />
          </Link>
        </Button>
      </CardHeader>
      <CardContent className="flex flex-1 flex-col gap-4">
        {loading ? (
          <Skeleton className="h-28 w-full" />
        ) : shown === 0 ? (
          <p className="text-sm text-muted-foreground">No stories on the graph yet.</p>
        ) : (
          <>
            <div className="flex items-baseline gap-2">
              <span className="text-3xl font-semibold tabular-nums tracking-tight">
                {(total ?? shown).toLocaleString()}
              </span>
              <span className="text-sm text-muted-foreground">credibility-scored stories</span>
            </div>
            <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
              {ordered.map(([band, info]) => (
                <div
                  key={band}
                  className={cn("h-full", bandTone(info.score, info.forecast))}
                  style={{ width: `${(info.count / shown) * 100}%` }}
                  title={`${band}: ${info.count}`}
                />
              ))}
            </div>
            <ul className="flex flex-col gap-1.5">
              {ordered.map(([band, info]) => (
                <li key={band} className="flex items-center justify-between gap-2 text-sm">
                  <span className="flex items-center gap-2">
                    <span className={cn("size-2 rounded-full", bandTone(info.score, info.forecast))} />
                    <span className="capitalize">{band}</span>
                  </span>
                  <span className="tabular-nums text-muted-foreground">{info.count}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function NeedsAttentionCard({ stories, loading }: { stories: Story[] | undefined; loading: boolean }) {
  const flagged = (stories ?? [])
    .map((s) => ({ story: s, reason: attentionReason(s) }))
    .filter((x): x is { story: Story; reason: string } => x.reason !== null)
    .slice(0, 6);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2 space-y-0">
        <CardTitle className="flex items-center gap-2 text-base">
          <ScrollText className="size-4" /> Needs your attention
        </CardTitle>
        {!loading && (
          <Button asChild variant="ghost" size="sm">
            <Link href="/stories">
              All stories <ArrowRight className="size-3.5" />
            </Link>
          </Button>
        )}
      </CardHeader>
      <CardContent>
        {loading ? (
          <Skeleton className="h-24 w-full" />
        ) : flagged.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing flagged — every story is corroborated by more than one source.
          </p>
        ) : (
          <ul className="flex flex-col divide-y">
            {flagged.map(({ story, reason }) => (
              <li key={story.id} className="flex items-center justify-between gap-3 py-2.5 first:pt-0 last:pb-0">
                <div className="flex min-w-0 flex-col gap-1">
                  <Link
                    href="/stories"
                    className="line-clamp-1 text-sm font-medium underline-offset-4 hover:underline"
                  >
                    {story.headline}
                  </Link>
                  <div className="flex items-center gap-2">
                    <ScoreBadge
                      label={story.label}
                      score={story.score}
                      forecastOnly={story.forecast_only}
                      capped={story.capped}
                    />
                    <Badge variant="secondary" className="font-normal text-muted-foreground">
                      {reason}
                    </Badge>
                  </div>
                </div>
                {story.last_updated > 0 && (
                  <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                    {relativeTime(story.last_updated * 1000)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
