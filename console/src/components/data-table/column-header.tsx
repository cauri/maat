"use client";

import type { Column } from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** A sortable column header for use in `ColumnDef.header`. Falls back to plain text when the
 * column isn't sortable. */
export function ColumnHeader<TData, TValue>({
  column,
  title,
  className,
}: {
  column: Column<TData, TValue>;
  title: string;
  className?: string;
}) {
  if (!column.getCanSort()) {
    return <span className={cn("text-xs font-medium text-muted-foreground", className)}>{title}</span>;
  }
  const sorted = column.getIsSorted();
  return (
    <Button
      variant="ghost"
      size="xs"
      className={cn("-ml-2 text-muted-foreground hover:text-foreground", className)}
      onClick={() => column.toggleSorting(sorted === "asc")}
    >
      <span>{title}</span>
      {sorted === "asc" ? (
        <ArrowUp />
      ) : sorted === "desc" ? (
        <ArrowDown />
      ) : (
        <ChevronsUpDown className="opacity-50" />
      )}
    </Button>
  );
}
