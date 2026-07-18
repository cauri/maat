"use client";

// The Prompts room (#446) — one index of every prompt the engine runs, in cauri's cross-project
// hub pattern: grouped by state, described by WHAT EACH PROMPT SHAPES (never a slug hunt), with
// the editor and the version trail one click away. Saving is sign-off gated (prompt.update, the
// same audited command the Tuning tab used); restoring an old version is just saving its text, so
// history needs no new command and every restore is itself a new audited version.

import { useMemo, useState } from "react";

import { Check, History, RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useRunCommand } from "@/hooks/use-command";
import { usePrompt, usePrompts } from "@/hooks/use-tuning";
import type { PromptDetail, PromptSummary, PromptVersion } from "@/lib/types";
import { cn } from "@/lib/utils";

import { SignoffButton } from "@/components/tuning/signoff-button";

function statusTone(status: string): string {
  if (status === "active") return "bg-emerald-500";
  if (status === "draft") return "bg-amber-500";
  return "bg-muted-foreground/40"; // on-device
}

// The hub's groups, in reading order: what needs cauri first, then what runs live, then the gated
// drafts, then the read-only device mirrors.
function groupOf(p: PromptSummary): "review" | "active" | "draft" | "device" {
  if (p.needs_review && p.editable) return "review";
  if (p.status === "active") return "active";
  if (p.status === "draft") return "draft";
  return "device";
}

const GROUPS: { id: ReturnType<typeof groupOf>; title: string; blurb: string }[] = [
  {
    id: "review",
    title: "Needs review",
    blurb: "Draft seeds shipped by the pipeline — running, awaiting your read.",
  },
  {
    id: "active",
    title: "Pipeline",
    blurb: "Live on every tick — the biggest levers on how the engine reads.",
  },
  {
    id: "draft",
    title: "Drafts",
    blurb: "Gated features and console personas — editable, live where their gate is on.",
  },
  {
    id: "device",
    title: "On-device",
    blurb: "Apple-client mirrors — read-only here, shipped with the app.",
  },
];

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function VersionRow({
  v,
  promptKey,
  editable,
}: {
  v: PromptVersion;
  promptKey: string;
  editable: boolean;
}) {
  const run = useRunCommand([["prompts"], ["prompt", promptKey]]);
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-md border border-border/60">
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 text-xs">
        <span className="font-mono font-medium">v{v.version}</span>
        {v.active && (
          <Badge variant="secondary" className="h-4 px-1.5 font-normal">
            live
          </Badge>
        )}
        <span className="text-muted-foreground">
          {v.actor || "operator"}
          {v.reason ? ` — ${v.reason}` : ""}
        </span>
        <span className="ml-auto text-muted-foreground">{timeAgo(v.created_at)}</span>
        <Button variant="ghost" size="sm" className="h-6 px-2" onClick={() => setOpen(!open)}>
          {open ? "Hide" : "View"}
        </Button>
        {editable && !v.active && (
          <SignoffButton
            label="Restore"
            title={`Restore v${v.version} of ${promptKey}`}
            disabled={run.isPending}
            description={
              <>
                <p>
                  Make v{v.version} the live <code className="font-mono">{promptKey}</code> prompt
                  again. It takes effect immediately and is recorded as a new version.
                </p>
                <p className="text-muted-foreground">
                  Needs your sign-off because it changes how the engine reads and judges.
                </p>
              </>
            }
            onConfirm={() =>
              run.mutate({
                name: "prompt.update",
                body: {
                  key: promptKey,
                  text: v.text,
                  reason: `restore v${v.version} from console`,
                },
              })
            }
          />
        )}
      </div>
      {open && (
        <pre className="max-h-64 overflow-auto border-t border-border/60 bg-muted/30 p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap">
          {v.text}
        </pre>
      )}
    </div>
  );
}

function PromptBody({ detail, summary }: { detail: PromptDetail; summary?: PromptSummary }) {
  const run = useRunCommand([["prompts"], ["prompt", detail.key]]);
  const [text, setText] = useState(detail.text);
  const [showHistory, setShowHistory] = useState(false);
  const dirty = text !== detail.text;
  const atDefault = text.trim() === detail.default.trim();
  const history = detail.versions ?? [];

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={cn("size-2 rounded-full", statusTone(detail.status))} />
          <code className="font-mono text-sm">{detail.key}</code>
          <Badge variant="secondary" className="font-normal capitalize">
            {detail.status}
          </Badge>
          {summary?.needs_review && (
            <Badge className="border-0 bg-amber-500/15 font-normal text-amber-600 dark:text-amber-400">
              needs review
            </Badge>
          )}
        </div>
        {!detail.editable && (
          <span className="text-xs text-muted-foreground">read-only (on-device mirror)</span>
        )}
      </div>

      {detail.description && (
        <p className="text-sm text-muted-foreground">{detail.description}</p>
      )}
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        {detail.source && <code className="font-mono">{detail.source}</code>}
        {detail.placeholders?.map((ph) => (
          <Badge key={ph} variant="outline" className="h-5 px-1.5 font-mono text-[10px] font-normal">
            {ph}
          </Badge>
        ))}
      </div>

      <Textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        disabled={!detail.editable}
        spellCheck={false}
        className="min-h-80 flex-1 resize-none font-mono text-xs leading-relaxed"
      />

      {detail.editable && (
        <div className="flex flex-wrap items-center gap-2">
          <SignoffButton
            label="Save"
            title={`Save the ${detail.key} prompt`}
            disabled={run.isPending || !dirty}
            description={
              <>
                <p>
                  Make this the live prompt the <code className="font-mono">{detail.key}</code>{" "}
                  agent runs. It takes effect immediately.
                </p>
                <p className="text-muted-foreground">
                  Needs your sign-off because it changes how the engine reads and judges. The
                  change is recorded as a new version.
                </p>
              </>
            }
            onConfirm={() =>
              run.mutate({
                name: "prompt.update",
                body: { key: detail.key, text, reason: "edited in console" },
              })
            }
          />
          {summary?.needs_review && (
            <Button
              variant="outline"
              size="sm"
              disabled={run.isPending}
              onClick={() =>
                run.mutate({
                  name: "prompt.reviewed",
                  body: { key: detail.key, reason: "reviewed in console" },
                })
              }
            >
              <Check /> Mark reviewed
            </Button>
          )}
          {!atDefault && (
            <Button
              variant="ghost"
              size="sm"
              disabled={run.isPending}
              onClick={() => setText(detail.default)}
              title="Reset the editor to the code default (not saved until you Save)"
            >
              <RotateCcw /> Reset to default
            </Button>
          )}
          {history.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="ml-auto"
              onClick={() => setShowHistory(!showHistory)}
            >
              <History /> History ({history.length})
            </Button>
          )}
        </div>
      )}

      {showHistory && history.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <p className="text-xs text-muted-foreground">
            Every saved version, newest first — the audited trail. Restoring makes an old text
            live again as a new version; nothing is ever overwritten.
          </p>
          {history.map((v) => (
            <VersionRow key={v.version} v={v} promptKey={detail.key} editable={detail.editable} />
          ))}
        </div>
      )}
    </div>
  );
}

export function PromptsRoom() {
  const { data: list, isLoading } = usePrompts();
  const [selected, setSelected] = useState<string | null>(null);
  const detail = usePrompt(selected);
  const summary = list?.prompts.find((p) => p.key === selected);

  const grouped = useMemo(() => {
    const buckets = new Map<string, PromptSummary[]>();
    for (const p of list?.prompts ?? []) {
      const g = groupOf(p);
      buckets.set(g, [...(buckets.get(g) ?? []), p]);
    }
    return buckets;
  }, [list]);

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-4 p-4 sm:p-6">
      <p className="text-sm text-muted-foreground">
        Every prompt the engine runs, in one place. Edits go live immediately after your sign-off
        and are recorded as versions — nothing is ever overwritten, and any version can be
        restored.
      </p>
      <div className="grid gap-4 lg:grid-cols-[300px_1fr]">
        <div className="flex flex-col gap-3">
          {isLoading ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            GROUPS.map((g) => {
              const rows = grouped.get(g.id) ?? [];
              if (rows.length === 0) return null;
              return (
                <Card key={g.id}>
                  <CardContent className="flex flex-col gap-0.5 p-2">
                    <div className="px-2.5 pt-1 pb-1.5">
                      <p
                        className={cn(
                          "text-xs font-medium",
                          g.id === "review" ? "text-amber-600 dark:text-amber-400" : "",
                        )}
                      >
                        {g.title}
                        {g.id === "review" && ` (${rows.length})`}
                      </p>
                      <p className="text-[11px] leading-snug text-muted-foreground">{g.blurb}</p>
                    </div>
                    {rows.map((p) => (
                      <button
                        key={p.key}
                        type="button"
                        onClick={() => setSelected(p.key)}
                        className={cn(
                          "flex flex-col gap-0.5 rounded-md px-2.5 py-1.5 text-left outline-none transition-colors",
                          selected === p.key ? "bg-muted" : "hover:bg-muted/60",
                        )}
                      >
                        <span className="flex items-center gap-2 text-sm">
                          <span className={cn("size-2 shrink-0 rounded-full", statusTone(p.status))} />
                          <span className="flex-1 truncate">{p.label}</span>
                          {p.needs_review && (
                            <span className="size-1.5 shrink-0 rounded-full bg-amber-500" />
                          )}
                        </span>
                        {p.description && (
                          <span className="line-clamp-2 pl-4 text-[11px] leading-snug text-muted-foreground">
                            {p.description}
                          </span>
                        )}
                      </button>
                    ))}
                  </CardContent>
                </Card>
              );
            })
          )}
        </div>

        <Card>
          <CardContent className="min-h-96 p-3 sm:p-4">
            {selected == null ? (
              <p className="grid h-full place-items-center text-sm text-muted-foreground">
                Select a prompt to review or edit it.
              </p>
            ) : detail.isLoading ? (
              <Skeleton className="h-80 w-full" />
            ) : detail.data ? (
              <PromptBody key={detail.data.key} detail={detail.data} summary={summary} />
            ) : null}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
