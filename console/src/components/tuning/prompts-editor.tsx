"use client";

import { useState } from "react";

import { Check, RotateCcw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useRunCommand } from "@/hooks/use-command";
import { usePrompt, usePrompts } from "@/hooks/use-tuning";
import type { PromptDetail, PromptSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

import { SignoffButton } from "./signoff-button";

function statusTone(status: string): string {
  if (status === "active") return "bg-emerald-500";
  if (status === "draft") return "bg-amber-500";
  return "bg-muted-foreground/40"; // on-device
}

function PromptBody({ detail, summary }: { detail: PromptDetail; summary?: PromptSummary }) {
  const run = useRunCommand([["prompts"], ["prompt", detail.key]]);
  const [text, setText] = useState(detail.text);
  const dirty = text !== detail.text;
  const atDefault = text.trim() === detail.default.trim();

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
                  Make this the live prompt the <code className="font-mono">{detail.key}</code> agent
                  runs. It takes effect immediately.
                </p>
                <p className="text-muted-foreground">
                  Needs your sign-off because it changes how the engine reads and judges. The change
                  is recorded.
                </p>
              </>
            }
            onConfirm={() =>
              run.mutate({ name: "prompt.update", body: { key: detail.key, text, reason: "edited in console" } })
            }
          />
          {summary?.needs_review && (
            <Button
              variant="outline"
              size="sm"
              disabled={run.isPending}
              onClick={() =>
                run.mutate({ name: "prompt.reviewed", body: { key: detail.key, reason: "reviewed in console" } })
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
        </div>
      )}
    </div>
  );
}

export function PromptsEditor() {
  const { data: list, isLoading } = usePrompts();
  const [selected, setSelected] = useState<string | null>(null);
  const detail = usePrompt(selected);
  const summary = list?.prompts.find((p) => p.key === selected);

  return (
    <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
      <Card>
        <CardContent className="flex flex-col gap-0.5 p-2">
          {isLoading ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            list?.prompts.map((p) => (
              <button
                key={p.key}
                type="button"
                onClick={() => setSelected(p.key)}
                className={cn(
                  "flex items-center gap-2 rounded-md px-2.5 py-1.5 text-left text-sm outline-none transition-colors",
                  selected === p.key ? "bg-muted" : "hover:bg-muted/60",
                )}
              >
                <span className={cn("size-2 shrink-0 rounded-full", statusTone(p.status))} />
                <span className="flex-1 truncate">{p.label}</span>
                {p.needs_review && <span className="size-1.5 shrink-0 rounded-full bg-amber-500" />}
              </button>
            ))
          )}
        </CardContent>
      </Card>

      <Card>
        <CardContent className="min-h-96 p-3">
          {selected == null ? (
            <p className="grid h-full place-items-center text-sm text-muted-foreground">
              Select a prompt to view and edit it.
            </p>
          ) : detail.isLoading ? (
            <Skeleton className="h-80 w-full" />
          ) : detail.data ? (
            <PromptBody key={detail.data.key} detail={detail.data} summary={summary} />
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
