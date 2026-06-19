"use client";

import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { StreamStatus } from "@/hooks/use-event-stream";
import { cn } from "@/lib/utils";

import { useShell } from "./shell-context";

const META: Record<StreamStatus, { label: string; dot: string; pulse: boolean; hint: string }> = {
  live: {
    label: "Live",
    dot: "bg-emerald-500",
    pulse: false,
    hint: "Connected to the event stream — projections update in real time.",
  },
  connecting: {
    label: "Connecting",
    dot: "bg-amber-500",
    pulse: true,
    hint: "Opening the event stream…",
  },
  offline: {
    label: "Offline",
    dot: "bg-muted-foreground/60",
    pulse: false,
    hint: "No live stream. The command/query API (#304) isn't reachable yet — retrying.",
  },
  idle: {
    label: "Idle",
    dot: "bg-muted-foreground/40",
    pulse: false,
    hint: "Live updates are disabled (no stream URL configured).",
  },
};

export function LiveStatus({ className }: { className?: string }) {
  const { stream } = useShell();
  const meta = META[stream.status];
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "inline-flex select-none items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs text-muted-foreground",
            className,
          )}
        >
          <span className={cn("size-1.5 rounded-full", meta.dot, meta.pulse && "animate-pulse")} />
          {meta.label}
        </span>
      </TooltipTrigger>
      <TooltipContent>{meta.hint}</TooltipContent>
    </Tooltip>
  );
}
