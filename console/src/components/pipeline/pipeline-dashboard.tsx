"use client";

import {
  Activity,
  AlertTriangle,
  CircleCheck,
  CirclePause,
  Gauge,
  Play,
  RefreshCw,
  Skull,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useOverview } from "@/hooks/use-overview";
import { usePipeline } from "@/hooks/use-pipeline";
import { useRunCommand } from "@/hooks/use-command";
import { relativeTime } from "@/lib/time";
import type { PipelineStage } from "@/lib/types";
import { cn } from "@/lib/utils";

const CLOCKS = [
  { key: "ingestion", label: "Ingestion" },
  { key: "extraction", label: "Extraction" },
  { key: "corroboration", label: "Corroboration" },
] as const;

function freshnessTone(freshness: string): string {
  if (freshness === "fresh") return "text-emerald-600 dark:text-emerald-500";
  if (freshness === "stale") return "text-amber-600 dark:text-amber-500";
  return "text-rose-600 dark:text-rose-500";
}

function statusMeta(status: string): { label: string; tone: string } {
  if (status === "healthy" || status === "ok")
    return { label: "Healthy", tone: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" };
  if (status === "stalled")
    return { label: "Stalled", tone: "bg-rose-500/15 text-rose-600 dark:text-rose-400" };
  return { label: status, tone: "bg-amber-500/15 text-amber-600 dark:text-amber-400" };
}

export function PipelineDashboard() {
  const pipeline = usePipeline();
  const overview = useOverview();
  const run = useRunCommand([["pipeline"], ["overview"]]);
  const clock = useRunCommand([["overview"]]);
  const data = pipeline.data;

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          {data && <Badge className={cn("gap-1 border-0", statusMeta(data.status).tone)}>{statusMeta(data.status).label}</Badge>}
          {data && (
            <span className="text-xs text-muted-foreground">updated {relativeTime(Date.parse(data.as_of))}</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => pipeline.refetch()} disabled={pipeline.isFetching}>
            <RefreshCw className={pipeline.isFetching ? "animate-spin" : undefined} /> Refresh
          </Button>
          <Button
            size="sm"
            onClick={() => run.mutate({ name: "run.trigger", body: { stage: "pipeline", reason: "manual run from console" } })}
            disabled={run.isPending}
          >
            <Play /> Run pipeline
          </Button>
        </div>
      </div>

      {data && data.alerts.length > 0 && (
        <Card className="border-amber-500/30 bg-amber-500/5">
          <CardContent className="flex flex-col gap-1.5 py-3">
            {data.alerts.map((a) => (
              <p key={a} className="flex items-start gap-2 text-sm text-amber-700 dark:text-amber-400">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" /> {a}
              </p>
            ))}
          </CardContent>
        </Card>
      )}

      <section className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {pipeline.isLoading
          ? Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-28" />)
          : data?.stages.map((s) => <StageCard key={s.stage} stage={s} />)}
      </section>

      <section className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader className="space-y-0 pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <Activity className="size-4" /> Throughput
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            {data ? (
              <>
                <Row label="Newest event" value={data.throughput.newest_event_at ? relativeTime(Date.parse(data.throughput.newest_event_at)) : "never"} tone={freshnessTone(data.throughput.freshness)} />
                <Row label="Articles" value={data.projections.articles.toLocaleString()} />
                <Row label="Claims" value={data.projections.claims.toLocaleString()} />
                <Row label="Clusters" value={data.projections.clusters.toLocaleString()} />
              </>
            ) : (
              <Skeleton className="h-20" />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="space-y-0 pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <Skull className="size-4" /> Dead letters
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            {data ? (
              data.dead_letters.total === 0 ? (
                <p className="text-emerald-600 dark:text-emerald-500">None — clean.</p>
              ) : (
                <>
                  <Badge variant="destructive" className="self-start">{data.dead_letters.total} failed</Badge>
                  {data.dead_letters.recent.slice(0, 4).map((d, i) => (
                    <div key={i} className="flex flex-col">
                      <code className="font-mono text-xs">{d.type}</code>
                      <span className="line-clamp-1 text-xs text-muted-foreground">{d.error}</span>
                    </div>
                  ))}
                </>
              )
            ) : (
              <Skeleton className="h-20" />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="space-y-0 pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <Gauge className="size-4" /> Calibration
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            {data ? (
              <>
                <Row label="Mean confidence" value={data.calibration.mean_confidence.toFixed(2)} />
                <Row label="Well corroborated" value={String(data.calibration.well_corroborated)} />
                <Row label="Thinly sourced" value={String(data.calibration.thinly_sourced)} />
                <Row label="Has primary" value={String(data.calibration.has_primary_count)} />
              </>
            ) : (
              <Skeleton className="h-20" />
            )}
          </CardContent>
        </Card>
      </section>

      <Card>
        <CardHeader className="space-y-0 pb-2">
          <CardTitle className="text-base">Clocks</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap gap-2">
          {overview.data
            ? CLOCKS.map((c) => {
                const paused = overview.data!.clocks[c.key];
                return (
                  <Button
                    key={c.key}
                    variant="outline"
                    size="sm"
                    disabled={clock.isPending}
                    onClick={() =>
                      clock.mutate({
                        name: "clock.set",
                        body: { clock: c.key, paused: !paused, reason: paused ? "resumed from console" : "paused from console" },
                      })
                    }
                    className="gap-1.5"
                  >
                    {paused ? <CirclePause className="text-muted-foreground" /> : <CircleCheck className="text-emerald-500" />}
                    {c.label}: {paused ? "Paused" : "Running"}
                  </Button>
                );
              })
            : CLOCKS.map((c) => <Skeleton key={c.key} className="h-7 w-36" />)}
        </CardContent>
      </Card>
    </div>
  );
}

function StageCard({ stage }: { stage: PipelineStage }) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-1 py-4">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{stage.stage}</span>
        <span className="text-2xl font-semibold tabular-nums">{stage.count.toLocaleString()}</span>
        <span className={cn("text-xs font-medium", freshnessTone(stage.freshness))}>
          {stage.last_seen ? relativeTime(Date.parse(stage.last_seen)) : "never"}
          {stage.freshness !== "fresh" && ` · ${stage.freshness}`}
        </span>
      </CardContent>
    </Card>
  );
}

function Row({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn("font-medium tabular-nums", tone)}>{value}</span>
    </div>
  );
}
