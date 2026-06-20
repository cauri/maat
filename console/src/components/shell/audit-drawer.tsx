"use client";

import { useMemo } from "react";

import { ScrollText } from "lucide-react";

import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useAudit } from "@/hooks/use-audit";
import { relativeTime } from "@/lib/time";

import { LiveStatus } from "./live-status";
import { useShell } from "./shell-context";

interface AuditRow {
  key: string;
  type: string;
  actor: string | null;
  reason: string | null;
  ts: number;
  live: boolean;
}

export function AuditDrawer() {
  const { audit, stream } = useShell();
  const backfill = useAudit(audit.open);

  const rows = useMemo<AuditRow[]>(() => {
    const live: AuditRow[] = stream.events
      .filter((e) => e.type.startsWith("admin."))
      .map((e) => ({
        key: `live-${e.key}`,
        type: e.type,
        actor: e.actor ?? null,
        reason: typeof e.data?.reason === "string" ? (e.data.reason as string) : null,
        ts: e.ts,
        live: true,
      }));
    const history: AuditRow[] = (backfill.data?.events ?? []).map((e, i) => ({
      key: `bf-${i}-${e.stream_id}`,
      type: e.type,
      actor: e.actor,
      reason: e.reason,
      ts: Date.parse(e.at),
      live: false,
    }));
    // Dedup an event that's both just-emitted (live) and already folded (history).
    const seen = new Set<string>();
    return [...live, ...history]
      .filter((r) => {
        const id = `${r.type}|${r.reason ?? ""}|${Math.round(r.ts / 2000)}`;
        if (seen.has(id)) return false;
        seen.add(id);
        return true;
      })
      .sort((a, b) => b.ts - a.ts);
  }, [stream.events, backfill.data]);

  return (
    <Sheet open={audit.open} onOpenChange={audit.set}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-md">
        <SheetHeader className="border-b">
          <div className="flex items-center justify-between gap-2">
            <SheetTitle className="flex items-center gap-2">
              <ScrollText className="size-4" /> Audit log
            </SheetTitle>
            <LiveStatus />
          </div>
          <SheetDescription>
            Every operator action — yours and Sia&apos;s — as it happens, plus the full history.
          </SheetDescription>
        </SheetHeader>

        {rows.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 px-8 text-center">
            <ScrollText className="size-8 text-muted-foreground/40" />
            <p className="text-sm font-medium">
              {backfill.isLoading ? "Loading…" : "No activity yet"}
            </p>
            <p className="max-w-xs text-xs text-muted-foreground">
              Operator actions — corrections, source flags, setting changes, prompt edits — appear
              here as they happen, each recorded and tracked.
            </p>
          </div>
        ) : (
          <ScrollArea className="min-h-0 flex-1">
            <ul className="divide-y">
              {rows.map((row) => (
                <li key={row.key} className="flex flex-col gap-1 px-4 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <code className="truncate font-mono text-xs">{row.type}</code>
                    <time
                      className="flex shrink-0 items-center gap-1 text-xs text-muted-foreground"
                      dateTime={new Date(row.ts).toISOString()}
                    >
                      {row.live && <span className="size-1.5 rounded-full bg-emerald-500" />}
                      {relativeTime(row.ts)}
                    </time>
                  </div>
                  {row.reason && <p className="text-xs">{row.reason}</p>}
                  {row.actor && <p className="text-xs text-muted-foreground">by {row.actor}</p>}
                </li>
              ))}
            </ul>
          </ScrollArea>
        )}
      </SheetContent>
    </Sheet>
  );
}
