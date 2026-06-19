"use client";

import { useState } from "react";

import type { ColumnDef } from "@tanstack/react-table";
import { RefreshCw } from "lucide-react";

import { ColumnHeader } from "@/components/data-table/column-header";
import { DataTable } from "@/components/data-table/data-table";
import { useShell } from "@/components/shell/shell-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useClaims } from "@/hooks/use-claims";
import { ApiError } from "@/lib/api";
import { relativeTime } from "@/lib/time";
import type { Claim } from "@/lib/types";

import { ClaimWorkspace } from "./claim-workspace";

const PAGE = 200;

const columns: ColumnDef<Claim>[] = [
  {
    accessorKey: "text",
    header: ({ column }) => <ColumnHeader column={column} title="Claim" />,
    cell: ({ row }) => <span className="line-clamp-2 max-w-xl">{row.original.text}</span>,
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

export function ClaimsTable() {
  const { data, isLoading, error, isFetching, refetch } = useClaims(PAGE);
  const { setSelection } = useShell();
  const [activeId, setActiveId] = useState<string | null>(null);

  const total = data?.total ?? 0;
  const truncated = total > (data?.claims.length ?? 0);

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
          {truncated
            ? `Newest ${data?.claims.length} of ${total} claims`
            : `${total} claims`}{" "}
          — the article→claim firehose. Open one to inspect provenance and correct it.
        </p>
        <Button variant="ghost" size="sm" onClick={() => refetch()} disabled={isFetching} aria-label="Refresh">
          <RefreshCw className={isFetching ? "animate-spin" : undefined} /> Refresh
        </Button>
      </div>

      <div className="min-h-0 flex-1">
        <DataTable
          tableId="claims"
          columns={columns}
          data={data?.claims ?? []}
          getRowId={(c) => c.id}
          isLoading={isLoading}
          error={error ? (error instanceof ApiError ? error.message : "Failed to load claims") : null}
          searchPlaceholder="Search claims…"
          onRowClick={openClaim}
          activeRowId={activeId ?? undefined}
          emptyMessage="No claims yet — they appear as articles are extracted."
        />
      </div>

      <ClaimWorkspace claimId={activeId} onClose={closeClaim} />
    </div>
  );
}
