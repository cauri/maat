"use client";

import { type KeyboardEvent, useMemo, useState } from "react";

import {
  type ColumnDef,
  type RowSelectionState,
  type SortingState,
  type Updater,
  type VisibilityState,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { Search, SlidersHorizontal, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { usePersistentState } from "@/hooks/use-persistent-state";
import { cn } from "@/lib/utils";

interface PersistedView {
  sorting: SortingState;
  columnVisibility: VisibilityState;
}

interface DataTableProps<TData, TValue> {
  columns: ColumnDef<TData, TValue>[];
  data: TData[];
  /** Stable id — namespaces the persisted view (sort + column visibility). */
  tableId: string;
  getRowId?: (row: TData) => string;
  isLoading?: boolean;
  error?: string | null;
  searchPlaceholder?: string;
  onRowClick?: (row: TData) => void;
  /** Highlights the open row (e.g. while its workspace drawer is shown). */
  activeRowId?: string;
  /** Extra controls on the right of the toolbar (filters, bulk actions). */
  toolbar?: React.ReactNode;
  emptyMessage?: string;
  pageSize?: number;
  /** Prepend a selection checkbox column for bulk actions. */
  enableSelection?: boolean;
}

function selectionColumn<TData, TValue>(): ColumnDef<TData, TValue> {
  return {
    id: "select",
    enableSorting: false,
    enableHiding: false,
    size: 36,
    header: ({ table }) => (
      <Checkbox
        checked={
          table.getIsAllPageRowsSelected() || (table.getIsSomePageRowsSelected() && "indeterminate")
        }
        onCheckedChange={(value) => table.toggleAllPageRowsSelected(!!value)}
        aria-label="Select all"
        onClick={(e) => e.stopPropagation()}
      />
    ),
    cell: ({ row }) => (
      <Checkbox
        checked={row.getIsSelected()}
        onCheckedChange={(value) => row.toggleSelected(!!value)}
        aria-label="Select row"
        onClick={(e) => e.stopPropagation()}
      />
    ),
  };
}

/**
 * The shared live data-grid (#305): instant sort, search, column show/hide with a persisted
 * "saved view", row selection, keyboard navigation, and client pagination. Consumed by every
 * list room (Stories, Sources, Claims, Feedback, Business).
 */
export function DataTable<TData, TValue>({
  columns,
  data,
  tableId,
  getRowId,
  isLoading = false,
  error = null,
  searchPlaceholder = "Search…",
  onRowClick,
  activeRowId,
  toolbar,
  emptyMessage = "Nothing here yet.",
  pageSize = 25,
  enableSelection = false,
}: DataTableProps<TData, TValue>) {
  const finalColumns = useMemo(
    () => (enableSelection ? [selectionColumn<TData, TValue>(), ...columns] : columns),
    [columns, enableSelection],
  );
  const [view, setView] = usePersistentState<PersistedView>(`maat.table.${tableId}`, {
    sorting: [],
    columnVisibility: {},
  });
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [globalFilter, setGlobalFilter] = useState("");

  const onSortingChange = (updater: Updater<SortingState>) =>
    setView({
      ...view,
      sorting: typeof updater === "function" ? updater(view.sorting) : updater,
    });
  const onColumnVisibilityChange = (updater: Updater<VisibilityState>) =>
    setView({
      ...view,
      columnVisibility:
        typeof updater === "function" ? updater(view.columnVisibility) : updater,
    });

  // TanStack Table manages its own memoization; its instance is intentionally not React-memoizable.
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data,
    columns: finalColumns,
    state: {
      sorting: view.sorting,
      columnVisibility: view.columnVisibility,
      rowSelection,
      globalFilter,
    },
    getRowId,
    enableRowSelection: true,
    onSortingChange,
    onColumnVisibilityChange,
    onRowSelectionChange: setRowSelection,
    onGlobalFilterChange: setGlobalFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize } },
  });

  const hideable = table.getAllColumns().filter((c) => c.getCanHide());
  const rows = table.getRowModel().rows;
  const selectedCount = table.getSelectedRowModel().rows.length;

  const onRowKeyDown = (event: KeyboardEvent<HTMLTableRowElement>, row: TData) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onRowClick?.(row);
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const el = event.currentTarget;
      const sib = event.key === "ArrowDown" ? el.nextElementSibling : el.previousElementSibling;
      if (sib instanceof HTMLElement) sib.focus();
    }
  };

  return (
    <div className="flex h-full flex-col gap-3">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative max-w-xs flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={globalFilter}
            onChange={(e) => setGlobalFilter(e.target.value)}
            placeholder={searchPlaceholder}
            className="h-8 pl-8"
            aria-label="Search table"
          />
          {globalFilter && (
            <button
              type="button"
              onClick={() => setGlobalFilter("")}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:text-foreground"
              aria-label="Clear search"
            >
              <X className="size-3.5" />
            </button>
          )}
        </div>

        <div className="ml-auto flex items-center gap-2">
          {selectedCount > 0 && (
            <span className="text-xs text-muted-foreground">{selectedCount} selected</span>
          )}
          {toolbar}
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="outline" size="sm">
                <SlidersHorizontal /> View
              </Button>
            </PopoverTrigger>
            <PopoverContent align="end" className="w-52 p-1.5">
              <p className="px-2 py-1.5 text-xs font-medium text-muted-foreground">Columns</p>
              {hideable.map((column) => (
                <label
                  key={column.id}
                  className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm capitalize hover:bg-muted"
                >
                  <input
                    type="checkbox"
                    className="accent-primary"
                    checked={column.getIsVisible()}
                    onChange={(e) => column.toggleVisibility(e.target.checked)}
                  />
                  {column.id}
                </label>
              ))}
            </PopoverContent>
          </Popover>
        </div>
      </div>

      {/* table */}
      <div className="min-h-0 flex-1 overflow-auto rounded-lg border">
        <Table>
          <TableHeader className="sticky top-0 z-10 bg-background/95 backdrop-blur">
            {table.getHeaderGroups().map((hg) => (
              <TableRow key={hg.id} className="hover:bg-transparent">
                {hg.headers.map((header) => (
                  <TableHead key={header.id} style={{ width: header.getSize() }}>
                    {header.isPlaceholder
                      ? null
                      : flexRender(header.column.columnDef.header, header.getContext())}
                  </TableHead>
                ))}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {isLoading ? (
              Array.from({ length: 8 }).map((_, i) => (
                <TableRow key={`sk-${i}`} className="hover:bg-transparent">
                  {table.getVisibleLeafColumns().map((col) => (
                    <TableCell key={col.id}>
                      <Skeleton className="h-4 w-full max-w-40" />
                    </TableCell>
                  ))}
                </TableRow>
              ))
            ) : error ? (
              <TableRow className="hover:bg-transparent">
                <TableCell
                  colSpan={table.getVisibleLeafColumns().length}
                  className="h-40 text-center text-sm text-destructive"
                >
                  {error}
                </TableCell>
              </TableRow>
            ) : rows.length === 0 ? (
              <TableRow className="hover:bg-transparent">
                <TableCell
                  colSpan={table.getVisibleLeafColumns().length}
                  className="h-40 text-center text-sm text-muted-foreground"
                >
                  {globalFilter ? "No matches." : emptyMessage}
                </TableCell>
              </TableRow>
            ) : (
              rows.map((row) => {
                const isActive = activeRowId != null && row.id === activeRowId;
                return (
                  <TableRow
                    key={row.id}
                    tabIndex={onRowClick ? 0 : undefined}
                    onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                    onKeyDown={onRowClick ? (e) => onRowKeyDown(e, row.original) : undefined}
                    data-state={row.getIsSelected() ? "selected" : undefined}
                    className={cn(
                      onRowClick && "cursor-pointer outline-none focus-visible:bg-muted",
                      isActive && "bg-muted",
                    )}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <TableCell key={cell.id}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </TableCell>
                    ))}
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {/* footer / pagination */}
      <div className="flex shrink-0 items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>
          {rows.length} of {table.getFilteredRowModel().rows.length} row
          {table.getFilteredRowModel().rows.length === 1 ? "" : "s"}
        </span>
        <div className="flex items-center gap-2">
          <span>
            Page {table.getState().pagination.pageIndex + 1} of {Math.max(1, table.getPageCount())}
          </span>
          <Button
            variant="outline"
            size="xs"
            onClick={() => table.previousPage()}
            disabled={!table.getCanPreviousPage()}
          >
            Prev
          </Button>
          <Button
            variant="outline"
            size="xs"
            onClick={() => table.nextPage()}
            disabled={!table.getCanNextPage()}
          >
            Next
          </Button>
        </div>
      </div>
    </div>
  );
}
