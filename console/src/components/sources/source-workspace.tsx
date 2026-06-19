"use client";

import { useState } from "react";

import { Ban, Link2, ShieldCheck } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useRunCommand } from "@/hooks/use-command";
import { relativeTime } from "@/lib/time";
import type { Source } from "@/lib/types";

import { ReliabilityTier } from "./reliability-tier";
import { Sparkline } from "./sparkline";

function when(value: string | null): string {
  return value ? relativeTime(Date.parse(value)) : "—";
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className="text-sm font-medium tabular-nums">{value}</span>
    </div>
  );
}

export function SourceWorkspace({
  source,
  onClose,
}: {
  source: Source | null;
  onClose: () => void;
}) {
  const run = useRunCommand([["sources"]]);
  const [group, setGroup] = useState("");

  if (!source) {
    return (
      <Sheet open={false} onOpenChange={(open) => !open && onClose()}>
        <SheetContent side="right" />
      </Sheet>
    );
  }

  const denied = source.status === "deny";
  const toggleFlag = () =>
    run.mutate({
      name: "source.flag",
      body: {
        source: source.source,
        status: denied ? "allow" : "deny",
        reason: denied ? "operator re-allowed" : "operator denied",
      },
    });
  const applyGroup = () => {
    const g = group.trim();
    if (!g) return;
    run.mutate(
      { name: "source.group", body: { source: source.source, group: g, reason: "operator grouping" } },
      { onSuccess: () => setGroup("") },
    );
  };

  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" className="w-full gap-0 overflow-y-auto p-0 sm:max-w-lg">
        <SheetHeader className="border-b">
          <SheetTitle className="pr-6 font-mono text-base">{source.source}</SheetTitle>
          <SheetDescription className="flex flex-wrap items-center gap-2">
            <ReliabilityTier reliability={source.reliability} />
            <Sparkline points={source.trajectory} width={72} />
            <Badge variant="secondary" className="font-normal capitalize">
              {source.state}
            </Badge>
            {denied && (
              <Badge variant="destructive" className="gap-1">
                <Ban className="size-3" /> Denied
              </Badge>
            )}
          </SheetDescription>
        </SheetHeader>

        <div className="flex flex-col gap-6 p-4">
          <section className="grid grid-cols-2 gap-4">
            <Stat label="Articles" value={source.articles.toLocaleString()} />
            <Stat
              label="Reliability (raw)"
              value={source.reliability != null ? source.reliability.toFixed(2) : "unrated"}
            />
            <Stat label="First seen" value={when(source.first_seen)} />
            <Stat label="Last seen" value={when(source.last_seen)} />
          </section>

          <section className="flex flex-col gap-3 border-t pt-4">
            <h3 className="text-sm font-medium">Operator actions</h3>
            <p className="text-xs text-muted-foreground">
              Every change is an audited <code>admin.source.*</code> event (D5/D28).
            </p>
            <Button
              variant={denied ? "outline" : "destructive"}
              size="sm"
              onClick={toggleFlag}
              disabled={run.isPending}
              className="justify-start"
            >
              {denied ? <ShieldCheck /> : <Ban />}
              {denied ? "Re-allow this source" : "Deny this source"}
            </Button>

            <div className="flex flex-col gap-1.5">
              <label htmlFor="group" className="text-xs text-muted-foreground">
                Group with an owner / wire (collapses co-owned outlets to one originator)
              </label>
              <div className="flex items-center gap-2">
                <Input
                  id="group"
                  value={group}
                  onChange={(e) => setGroup(e.target.value)}
                  placeholder="e.g. reuters-wire"
                  className="h-8"
                  onKeyDown={(e) => e.key === "Enter" && applyGroup()}
                />
                <Button size="sm" onClick={applyGroup} disabled={run.isPending || !group.trim()}>
                  <Link2 /> Group
                </Button>
              </div>
            </div>
          </section>
        </div>
      </SheetContent>
    </Sheet>
  );
}
