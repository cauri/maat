"use client";

import { useState } from "react";

import type { ColumnDef } from "@tanstack/react-table";
import { Ban, RefreshCw } from "lucide-react";

import { ColumnHeader } from "@/components/data-table/column-header";
import { DataTable, type Facet } from "@/components/data-table/data-table";
import { useShell } from "@/components/shell/shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useSources } from "@/hooks/use-sources";
import { ApiError } from "@/lib/api";
import { relativeTime } from "@/lib/time";
import type { Source } from "@/lib/types";
import { cn } from "@/lib/utils";

import { ReliabilityTier } from "./reliability-tier";
import { Sparkline } from "./sparkline";
import { SourceWorkspace } from "./source-workspace";

function stateDot(state: string): string {
  if (state === "active") return "bg-emerald-500";
  if (state === "scored") return "bg-sky-500";
  if (state === "backfilling" || state === "registered") return "bg-amber-500";
  return "bg-muted-foreground/40";
}

const columns: ColumnDef<Source>[] = [
  {
    accessorKey: "source",
    header: ({ column }) => <ColumnHeader column={column} title="Source" />,
    cell: ({ row }) => (
      <span className="flex items-center gap-2">
        <span
          className={cn("truncate font-medium", row.original.status === "deny" && "text-muted-foreground line-through")}
        >
          {row.original.source}
        </span>
        {row.original.status === "deny" && (
          <Badge variant="destructive" className="gap-1 px-1.5">
            <Ban className="size-3" /> denied
          </Badge>
        )}
      </span>
    ),
  },
  {
    accessorKey: "reliability",
    header: ({ column }) => <ColumnHeader column={column} title="Reliability" />,
    sortUndefined: "last",
    cell: ({ row }) => (
      <span className="flex items-center gap-2">
        <ReliabilityTier reliability={row.original.reliability} />
        <Sparkline points={row.original.trajectory} />
      </span>
    ),
  },
  {
    accessorKey: "articles",
    header: ({ column }) => <ColumnHeader column={column} title="Articles" />,
    cell: ({ row }) => <span className="tabular-nums">{row.original.articles.toLocaleString()}</span>,
  },
  {
    accessorKey: "state",
    header: ({ column }) => <ColumnHeader column={column} title="State" />,
    cell: ({ row }) => (
      <span className="flex items-center gap-1.5 capitalize">
        <span className={cn("size-2 rounded-full", stateDot(row.original.state))} />
        {row.original.state}
      </span>
    ),
  },
  {
    accessorKey: "last_seen",
    header: ({ column }) => <ColumnHeader column={column} title="Last seen" />,
    cell: ({ row }) => (
      <span className="text-muted-foreground">
        {row.original.last_seen ? relativeTime(Date.parse(row.original.last_seen)) : "—"}
      </span>
    ),
  },
];

const facets: Facet[] = [{ columnId: "state", label: "State" }];

export function SourcesTable() {
  const { data, isLoading, error, isFetching, refetch } = useSources();
  const { setSelection } = useShell();
  const [activeId, setActiveId] = useState<string | null>(null);

  const active = data?.sources.find((s) => s.source === activeId) ?? null;

  const openSource = (source: Source) => {
    setActiveId(source.source);
    setSelection({ kind: "source", id: source.source, label: source.source });
  };
  const closeSource = () => {
    setActiveId(null);
    setSelection(null);
  };

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          {data ? `${data.total} sources` : "Sources"} — one reliability read per outlet. Open one to
          deny it or group co-owned outlets.
        </p>
        <Button variant="ghost" size="sm" onClick={() => refetch()} disabled={isFetching} aria-label="Refresh">
          <RefreshCw className={isFetching ? "animate-spin" : undefined} /> Refresh
        </Button>
      </div>

      <div className="min-h-0 flex-1">
        <DataTable
          tableId="sources"
          columns={columns}
          data={data?.sources ?? []}
          facets={facets}
          getRowId={(s) => s.source}
          isLoading={isLoading}
          error={error ? (error instanceof ApiError ? error.message : "Failed to load sources") : null}
          searchPlaceholder="Search sources…"
          onRowClick={openSource}
          activeRowId={activeId ?? undefined}
          emptyMessage="No sources yet — they appear as articles are ingested."
        />
      </div>

      <SourceWorkspace key={active?.source ?? "none"} source={active} onClose={closeSource} />
    </div>
  );
}
