"use client";

import { useState } from "react";

import type { ColumnDef } from "@tanstack/react-table";
import { RefreshCw } from "lucide-react";

import { ColumnHeader } from "@/components/data-table/column-header";
import { DataTable } from "@/components/data-table/data-table";
import { Button } from "@/components/ui/button";
import { useStories } from "@/hooks/use-stories";
import { ApiError } from "@/lib/api";
import { relativeTime } from "@/lib/time";
import type { Story } from "@/lib/types";

import { ScoreBadge } from "./score-badge";
import { StoryWorkspace } from "./story-workspace";

const columns: ColumnDef<Story>[] = [
  {
    accessorKey: "headline",
    header: ({ column }) => <ColumnHeader column={column} title="Story" />,
    cell: ({ row }) => (
      <span className="line-clamp-2 max-w-xl font-medium">{row.original.headline}</span>
    ),
  },
  {
    accessorKey: "score",
    header: ({ column }) => <ColumnHeader column={column} title="Credibility" />,
    cell: ({ row }) => (
      <ScoreBadge
        label={row.original.label}
        score={row.original.score}
        forecastOnly={row.original.forecast_only}
        capped={row.original.capped}
      />
    ),
  },
  {
    accessorKey: "source_count",
    header: ({ column }) => <ColumnHeader column={column} title="Sources" />,
    cell: ({ row }) => <span className="tabular-nums">{row.original.source_count}</span>,
  },
  {
    accessorKey: "fact_count",
    header: ({ column }) => <ColumnHeader column={column} title="Facts" />,
    cell: ({ row }) => <span className="tabular-nums">{row.original.fact_count}</span>,
  },
  {
    accessorKey: "last_updated",
    header: ({ column }) => <ColumnHeader column={column} title="Updated" />,
    cell: ({ row }) => (
      <span className="whitespace-nowrap text-muted-foreground">
        {relativeTime(row.original.last_updated * 1000)}
      </span>
    ),
  },
];

export function StoriesTable() {
  const { data, isLoading, error, isFetching, refetch } = useStories();
  const [activeId, setActiveId] = useState<string | null>(null);

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          {data ? `${data.total} stories` : "Stories"} — the credibility read users see. Open one to
          verify and correct it.
        </p>
        <Button
          variant="ghost"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
          aria-label="Refresh"
        >
          <RefreshCw className={isFetching ? "animate-spin" : undefined} /> Refresh
        </Button>
      </div>

      <div className="min-h-0 flex-1">
        <DataTable
          tableId="stories"
          columns={columns}
          data={data?.stories ?? []}
          getRowId={(s) => s.id}
          isLoading={isLoading}
          error={error ? (error instanceof ApiError ? error.message : "Failed to load stories") : null}
          searchPlaceholder="Search stories…"
          onRowClick={(s) => setActiveId(s.id)}
          activeRowId={activeId ?? undefined}
          emptyMessage="No stories yet — once the pipeline corroborates clusters, they appear here."
        />
      </div>

      <StoryWorkspace nodeId={activeId} onClose={() => setActiveId(null)} />
    </div>
  );
}
