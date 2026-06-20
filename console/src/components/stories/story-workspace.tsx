"use client";

import { useState } from "react";

import { AlertTriangle, GitMerge, Layers, ShieldCheck } from "lucide-react";

import { TranslatedText } from "@/components/translated-text";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { useRunCommand } from "@/hooks/use-command";
import { useStory } from "@/hooks/use-stories";
import { relativeTime } from "@/lib/time";
import type { StoryFact } from "@/lib/types";
import { cn } from "@/lib/utils";

import { ScoreBadge } from "./score-badge";
import { TrajectorySparkline } from "./trajectory-sparkline";

export function StoryWorkspace({
  nodeId,
  onClose,
}: {
  nodeId: string | null;
  onClose: () => void;
}) {
  const { data: story, isLoading, error } = useStory(nodeId);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [reason, setReason] = useState("");
  const merge = useRunCommand([["stories"], ["story", nodeId]]);

  const toggle = (clusterId: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(clusterId)) next.delete(clusterId);
      else next.add(clusterId);
      return next;
    });

  const reset = () => {
    setSelected(new Set());
    setReason("");
    setConfirmOpen(false);
  };

  const doMerge = () =>
    merge.mutate(
      { name: "cluster.merge", body: { merged: [...selected], reason } },
      { onSuccess: reset },
    );

  return (
    <Sheet open={nodeId != null} onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-xl">
        <SheetHeader className="border-b">
          <SheetTitle className="pr-6 text-base leading-snug">
            {story ? (
              <TranslatedText text={story.headline} language={story.headline_lang} />
            ) : isLoading ? (
              "Loading story…"
            ) : (
              "Story"
            )}
          </SheetTitle>
          {story && (
            <SheetDescription className="flex flex-wrap items-center gap-2">
              <ScoreBadge
                label={story.label}
                score={story.score}
                forecastOnly={story.forecast_only}
                capped={story.capped}
              />
              <span>·</span>
              <span>
                {story.source_count} source{story.source_count === 1 ? "" : "s"} ·{" "}
                {story.cluster_count} cluster{story.cluster_count === 1 ? "" : "s"}
              </span>
            </SheetDescription>
          )}
        </SheetHeader>

        {error ? (
          <div className="p-6 text-sm text-destructive">Couldn&apos;t load this story.</div>
        ) : isLoading || !story ? (
          <div className="flex flex-col gap-3 p-6">
            <Skeleton className="h-5 w-2/3" />
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : (
          <Tabs defaultValue="reader" className="flex min-h-0 flex-1 flex-col gap-0">
            <TabsList className="mx-4 mt-3 self-start">
              <TabsTrigger value="reader">Reader</TabsTrigger>
              <TabsTrigger value="why">Derivation</TabsTrigger>
            </TabsList>

            {/* Reader — what users see, in plain language */}
            <TabsContent value="reader" className="min-h-0 flex-1 overflow-auto px-4 py-4">
              <div className="flex flex-col gap-5">
                {story.headline_orig && story.headline_orig !== story.headline && (
                  <p className="text-sm text-muted-foreground">
                    Original headline: <span className="italic">{story.headline_orig}</span>
                  </p>
                )}
                <div className="rounded-lg border bg-muted/30 p-4">
                  <p className="text-sm leading-relaxed">{story.why}</p>
                </div>
                <div>
                  <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Credibility over time
                  </p>
                  <TrajectorySparkline points={story.trajectory} />
                </div>
                <dl className="grid grid-cols-2 gap-3 text-sm">
                  <Stat label="Checkable facts" value={story.fact_count} />
                  <Stat label="Forecasts" value={story.forecast_count} />
                  <Stat label="First seen" value={relativeTime(story.first_seen * 1000)} />
                  <Stat label="Last updated" value={relativeTime(story.last_updated * 1000)} />
                </dl>
              </div>
            </TabsContent>

            {/* Derivation — the why, plus inline correction */}
            <TabsContent value="why" className="flex min-h-0 flex-1 flex-col">
              <ScrollArea className="min-h-0 flex-1">
                <div className="flex flex-col gap-2 px-4 py-3">
                  <p className="text-xs text-muted-foreground">
                    Each checkable fact, with how its confidence was derived. Select two or more facts
                    that are really the same to merge them.
                  </p>
                  {story.facts.map((fact) => (
                    <FactCard
                      key={fact.cluster_id}
                      fact={fact}
                      selected={selected.has(fact.cluster_id)}
                      onToggle={() => toggle(fact.cluster_id)}
                    />
                  ))}
                  {story.forecasts.length > 0 && (
                    <>
                      <p className="mt-3 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                        Forecasts — shown separately, never scored as truth
                      </p>
                      {story.forecasts.map((fact) => (
                        <FactCard key={fact.cluster_id} fact={fact} forecast />
                      ))}
                    </>
                  )}
                </div>
              </ScrollArea>

              {/* sticky correction bar */}
              <div className="flex items-center justify-between gap-2 border-t bg-background px-4 py-2.5">
                <span className="text-xs text-muted-foreground">
                  {selected.size > 0
                    ? `${selected.size} fact${selected.size === 1 ? "" : "s"} selected`
                    : "Select facts to correct"}
                </span>
                <Button
                  size="sm"
                  disabled={selected.size < 2}
                  onClick={() => setConfirmOpen(true)}
                >
                  <GitMerge /> Merge facts
                </Button>
              </div>
            </TabsContent>
          </Tabs>
        )}
      </SheetContent>

      {/* confirm — every correction is an audited command */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Merge {selected.size} facts</DialogTitle>
            <DialogDescription>
              They&apos;ll be treated as one fact. The change is recorded, takes effect on the next
              run, and can be undone.
            </DialogDescription>
          </DialogHeader>
          <Textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why are these the same fact? (recorded in the audit log)"
            rows={3}
          />
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="ghost" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button size="sm" onClick={doMerge} disabled={merge.isPending}>
              {merge.isPending ? "Merging…" : "Merge facts"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Sheet>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border px-3 py-2">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="font-medium">{value}</dd>
    </div>
  );
}

function FactCard({
  fact,
  selected = false,
  onToggle,
  forecast = false,
}: {
  fact: StoryFact;
  selected?: boolean;
  onToggle?: () => void;
  forecast?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex gap-2.5 rounded-lg border p-3 text-sm",
        selected && "border-primary/50 bg-primary/5",
      )}
    >
      {onToggle && (
        <Checkbox checked={selected} onCheckedChange={onToggle} className="mt-0.5" aria-label="Select fact" />
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex items-start justify-between gap-2">
          <p className="font-medium leading-snug">{fact.fact}</p>
          {fact.is_headline && !forecast && (
            <Badge variant="secondary" className="shrink-0">
              headline
            </Badge>
          )}
        </div>
        {fact.fact_en && fact.fact_en !== fact.fact && (
          <p className="text-xs text-muted-foreground">{fact.fact_en}</p>
        )}
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          {!forecast && <span>confidence {Math.round(fact.confidence * 100)}%</span>}
          <span className="inline-flex items-center gap-1">
            <Layers className="size-3" /> {fact.independent_originators} independent
          </span>
          {fact.has_primary && (
            <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
              <ShieldCheck className="size-3" /> primary source
            </span>
          )}
          {fact.disputed && (
            <span className="inline-flex items-center gap-1 text-rose-600 dark:text-rose-400">
              <AlertTriangle className="size-3" /> disputed
            </span>
          )}
          {fact.grounding && <span>grounding: {fact.grounding}</span>}
          {fact.extremity && fact.extremity !== "ordinary" && <span>{fact.extremity}</span>}
        </div>
        <p className="truncate text-xs text-muted-foreground">
          {fact.sources.map((s) => s.names.join("/")).join(" · ")}
        </p>
      </div>
    </div>
  );
}
