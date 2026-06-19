"use client";

import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useRunCommand } from "@/hooks/use-command";
import { useConfig } from "@/hooks/use-tuning";
import type { ConfigKnob } from "@/lib/types";

import { SignoffButton } from "./signoff-button";

function display(v: string | number | null): string {
  return v == null ? "—" : String(v);
}

function KnobRow({ knob }: { knob: ConfigKnob }) {
  const run = useRunCommand([["config"]]);
  const live = knob.active ?? knob.default;
  const [value, setValue] = useState(String(knob.proposed ?? live ?? ""));
  const dirty = value.trim() !== String(knob.proposed ?? live ?? "");

  if (!knob.enactable) {
    return (
      <div className="flex items-center justify-between gap-3 py-2.5">
        <div className="flex flex-col">
          <code className="font-mono text-sm">{knob.key}</code>
          <span className="text-xs text-muted-foreground">set in code · not hot-reloadable</span>
        </div>
        <span className="text-sm tabular-nums text-muted-foreground">{display(live)}</span>
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 py-2.5">
      <div className="flex min-w-0 flex-col">
        <code className="font-mono text-sm">{knob.key}</code>
        <span className="flex items-center gap-2 text-xs text-muted-foreground">
          live: {display(live)}
          {knob.active == null && <span>(default)</span>}
          {knob.proposed != null && (
            <Badge variant="secondary" className="gap-1 font-normal">
              proposed: {display(knob.proposed)}
            </Badge>
          )}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          className="h-8 w-28 tabular-nums"
        />
        <Button
          variant="outline"
          size="sm"
          disabled={run.isPending || !dirty}
          onClick={() =>
            run.mutate({ name: "config.set", body: { key: knob.key, value: value.trim(), reason: "proposed in console" } })
          }
        >
          Propose
        </Button>
        <SignoffButton
          label="Promote"
          title={`Promote ${knob.key}`}
          disabled={run.isPending}
          description={
            <>
              <p>
                Promote <code className="font-mono">{knob.key}</code> to <strong>{value.trim()}</strong>{" "}
                (live is {display(live)}).
              </p>
              <p className="text-muted-foreground">
                This enacts a veracity-core change immediately, audited as{" "}
                <code className="font-mono">admin.config.promoted</code>.
              </p>
            </>
          }
          onConfirm={() =>
            run.mutate({ name: "config.promote", body: { key: knob.key, value: value.trim(), reason: "promoted in console" } })
          }
        />
      </div>
    </div>
  );
}

export function ConfigKnobs() {
  const { data, isLoading } = useConfig();

  if (isLoading) return <Skeleton className="h-64 w-full" />;
  if (!data) return null;

  return (
    <div className="flex flex-col gap-4">
      {data.groups.map((group) => {
        const knobs = data.knobs.filter((k) => k.group === group);
        if (knobs.length === 0) return null;
        return (
          <Card key={group}>
            <CardHeader className="pb-1">
              <CardTitle className="text-base">{group}</CardTitle>
            </CardHeader>
            <CardContent className="divide-y py-0">
              {knobs.map((k) => (
                <KnobRow key={k.key} knob={k} />
              ))}
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
