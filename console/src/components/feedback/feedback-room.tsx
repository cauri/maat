"use client";

import { AlertTriangle, Ban, Check, RefreshCw, ShieldAlert } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useFeedback, useTriageFeedback } from "@/hooks/use-feedback";
import { relativeTime } from "@/lib/time";
import type { FeedbackItem } from "@/lib/types";

const ATTACK_THRESHOLD = 3;

export function FeedbackRoom() {
  const { data, isLoading, isFetching, refetch } = useFeedback();
  const triage = useTriageFeedback();
  const queue = data?.queue ?? [];

  // Coordinated-attack detection: a source drawing a burst of reports.
  const bySource = new Map<string, number>();
  for (const item of queue) bySource.set(item.source, (bySource.get(item.source) ?? 0) + 1);
  const attacks = [...bySource.entries()].filter(([, n]) => n >= ATTACK_THRESHOLD);

  const act = (item: FeedbackItem, category: string, route: string) =>
    triage.mutate({ itemId: item.item_id, category, route });

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4 p-4 sm:p-6">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          {queue.length} item{queue.length === 1 ? "" : "s"} awaiting triage — reader feedback routed
          to the operator.
        </p>
        <Button variant="ghost" size="sm" onClick={() => refetch()} disabled={isFetching}>
          <RefreshCw className={isFetching ? "animate-spin" : undefined} /> Refresh
        </Button>
      </div>

      {attacks.length > 0 && (
        <Card className="border-rose-500/30 bg-rose-500/5">
          <CardContent className="flex flex-col gap-1.5 py-3">
            <p className="flex items-center gap-2 text-sm font-medium text-rose-600 dark:text-rose-400">
              <ShieldAlert className="size-4" /> Possible coordinated attack
            </p>
            {attacks.map(([source, n]) => (
              <p key={source} className="text-sm text-rose-700 dark:text-rose-300">
                <span className="font-medium">{source}</span> — {n} reports in the queue. Review before
                acting on its reliability.
              </p>
            ))}
          </CardContent>
        </Card>
      )}

      {isLoading ? (
        <Skeleton className="h-64 w-full" />
      ) : queue.length === 0 ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-2 py-12 text-center">
            <Check className="size-8 text-emerald-500/60" />
            <p className="text-sm font-medium">Queue clear</p>
            <p className="max-w-xs text-xs text-muted-foreground">
              No feedback awaiting triage. Reader reports land here as they arrive.
            </p>
          </CardContent>
        </Card>
      ) : (
        <ul className="flex flex-col gap-3">
          {queue.map((item) => {
            const flagged = (bySource.get(item.source) ?? 0) >= ATTACK_THRESHOLD;
            return (
              <Card key={item.item_id}>
                <CardContent className="flex flex-col gap-3 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex min-w-0 flex-col gap-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">{item.source}</span>
                        {item.category_hint && (
                          <Badge variant="secondary" className="font-normal capitalize">
                            {item.category_hint}
                          </Badge>
                        )}
                        {flagged && (
                          <Badge className="gap-1 border-0 bg-rose-500/15 font-normal text-rose-600 dark:text-rose-400">
                            <AlertTriangle className="size-3" /> burst
                          </Badge>
                        )}
                      </div>
                      <p className="text-sm">{item.text}</p>
                    </div>
                    <time className="shrink-0 text-xs text-muted-foreground">
                      {item.submitted_at ? relativeTime(Date.parse(item.submitted_at)) : ""}
                    </time>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={triage.isPending}
                      onClick={() => act(item, "valid", "review")}
                    >
                      <Check /> Valid · review
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={triage.isPending}
                      onClick={() => act(item, "spam", "dismiss")}
                    >
                      <Ban /> Spam · dismiss
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={triage.isPending}
                      onClick={() => act(item, "coordinated", "flag")}
                    >
                      <ShieldAlert /> Coordinated · flag
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </ul>
      )}
    </div>
  );
}
