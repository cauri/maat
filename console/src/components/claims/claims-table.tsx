"use client";

import { useState } from "react";

import type { ColumnDef } from "@tanstack/react-table";
import { RefreshCw } from "lucide-react";

import { ColumnHeader } from "@/components/data-table/column-header";
import { DataTable, type Facet } from "@/components/data-table/data-table";
import { useShell } from "@/components/shell/shell-context";
import { TranslatedText } from "@/components/translated-text";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useClaims } from "@/hooks/use-claims";
import { ApiError } from "@/lib/api";
import { relativeTime } from "@/lib/time";
import type { Claim } from "@/lib/types";

import { ClaimWorkspace } from "./claim-workspace";

const columns: ColumnDef<Claim>[] = [
  {
    accessorKey: "text",
    header: ({ column }) => <ColumnHeader column={column} title="Claim" />,
    cell: ({ row }) => (
      <TranslatedText
        text={row.original.text}
        language={row.original.language}
        className="line-clamp-2 max-w-xl"
        glossClassName="line-clamp-2"
      />
    ),
  },
  {
    accessorKey: "kind",
    header: ({ column }) => <ColumnHeader column={column} title="Kind" />,
    cell: ({ row }) => (
      <Badge variant="secondary" className="font-normal capitalize">
        {row.original.kind}
      </Badge>
    ),
  },
  {
    accessorKey: "voice",
    header: ({ column }) => <ColumnHeader column={column} title="Voice" />,
    cell: ({ row }) => <span className="capitalize text-muted-foreground">{row.original.voice}</span>,
  },
  {
    accessorKey: "source",
    header: ({ column }) => <ColumnHeader column={column} title="Source" />,
    cell: ({ row }) => <span className="text-muted-foreground">{row.original.source}</span>,
  },
  {
    accessorKey: "created_at",
    header: ({ column }) => <ColumnHeader column={column} title="Extracted" />,
    cell: ({ row }) => (
      <span className="text-muted-foreground">{relativeTime(Date.parse(row.original.created_at))}</span>
    ),
  },
];

const facets: Facet[] = [
  { columnId: "kind", label: "Kind" },
  { columnId: "voice", label: "Voice" },
  { columnId: "source", label: "Source" },
];

export function ClaimsTable() {
  const { rows, total, isLoading, error, isFetching, refetch, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useClaims();
  const { setSelection } = useShell();
  const [activeId, setActiveId] = useState<string | null>(null);

  const openClaim = (claim: Claim) => {
    setActiveId(claim.id);
    setSelection({ kind: "claim", id: claim.id, label: claim.text.slice(0, 80) });
  };
  const closeClaim = () => {
    setActiveId(null);
    setSelection(null);
  };

  return (
    <div className="flex h-full flex-col gap-3 p-4">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm text-muted-foreground">
          {total ? `${total} claims` : "Claims"} — the article→claim firehose. Open one to inspect
          provenance and correct it.
        </p>
        <Button variant="ghost" size="sm" onClick={() => refetch()} disabled={isFetching} aria-label="Refresh">
          <RefreshCw className={isFetching ? "animate-spin" : undefined} /> Refresh
        </Button>
      </div>

      <div className="flex min-h-0 flex-1 gap-3">
        <div className="min-w-0 flex-1">
          <DataTable
            tableId="claims"
            columns={columns}
            data={rows}
            facets={facets}
            getRowId={(c) => c.id}
            isLoading={isLoading}
            error={error ? (error instanceof ApiError ? error.message : "Failed to load claims") : null}
            searchPlaceholder="Search claims…"
            onRowClick={openClaim}
            activeRowId={activeId ?? undefined}
            onLoadMore={fetchNextPage}
            hasMore={hasNextPage}
            isFetchingMore={isFetchingNextPage}
            emptyMessage="No claims yet — they appear as articles are extracted."
          />
        </div>

        <ClaimWorkspace key={activeId ?? "none"} claimId={activeId} onClose={closeClaim} />
      </div>
    </div>
  );
}
