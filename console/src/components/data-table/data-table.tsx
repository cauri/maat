"use client";

import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";

import {
  type Column,
  type ColumnDef,
  type ColumnFiltersState,
  type FilterFn,
  type RowSelectionState,
  type SortingState,
  type Updater,
  type VisibilityState,
  flexRender,
  getCoreRowModel,
  getFacetedRowModel,
  getFacetedUniqueValues,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { Filter, Loader2, Search, SlidersHorizontal, X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
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

/** A column the operator can filter by — rendered as a faceted multi-select in the toolbar. */
export interface Facet {
  /** The column's accessorKey / id. */
  columnId: string;
  label: string;
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
  /** Extra controls on the right of the toolbar (bulk actions). */
  toolbar?: React.ReactNode;
  emptyMessage?: string;
  /** Prepend a selection checkbox column for bulk actions. */
  enableSelection?: boolean;
  /** Faceted filters to offer in the toolbar. */
  facets?: Facet[];
  /** Infinite scroll: called when the sentinel scrolls into view and `hasMore` is true. */
  onLoadMore?: () => void;
  hasMore?: boolean;
  isFetchingMore?: boolean;
}

/** Multi-select facet filter: a row passes if its value is one of the selected. */
const facetFilterFn: FilterFn<unknown> = (row, columnId, value) => {
  const selected = value as string[] | undefined;
  if (!selected?.length) return true;
  return selected.includes(String(row.getValue(columnId)));
};

function selectionColumn<TData, TValue>(): ColumnDef<TData, TValue> {
  return {
    id: "select",
    enableSorting: false,
    enableHiding: false,
    size: 36,
    header: ({ table }) => (
      <Checkbox
        checked={
          table.getIsAllRowsSelected() || (table.getIsSomeRowsSelected() && "indeterminate")
        }
        onCheckedChange={(value) => table.toggleAllRowsSelected(!!value)}
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
 * The shared live data-grid (#305): instant sort, search, faceted filters, column show/hide
 * with a persisted "saved view", row selection, keyboard nav, and **infinite scroll** (lazy
 * loading via `onLoadMore`/`hasMore`). Consumed by every list room.
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
  enableSelection = false,
  facets = [],
  onLoadMore,
  hasMore = false,
  isFetchingMore = false,
}: DataTableProps<TData, TValue>) {
  const facetIds = useMemo(() => new Set(facets.map((f) => f.columnId)), [facets]);
  const finalColumns = useMemo(() => {
    const base = enableSelection ? [selectionColumn<TData, TValue>(), ...columns] : columns;
    // Inject the multi-select filter fn onto facet columns so rooms only declare which columns to facet.
    return base.map((c) => {
      const id = (c as { id?: string; accessorKey?: string }).id ??
        (c as { accessorKey?: string }).accessorKey;
      return id && facetIds.has(id) ? { ...c, filterFn: facetFilterFn as FilterFn<TData> } : c;
    });
  }, [columns, enableSelection, facetIds]);

  const [view, setView] = usePersistentState<PersistedView>(`maat.table.${tableId}`, {
    sorting: [],
    columnVisibility: {},
  });
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});
  const [globalFilter, setGlobalFilter] = useState("");
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);

  const onSortingChange = (updater: Updater<SortingState>) =>
    setView({ ...view, sorting: typeof updater === "function" ? updater(view.sorting) : updater });
  const onColumnVisibilityChange = (updater: Updater<VisibilityState>) =>
    setView({
      ...view,
      columnVisibility: typeof updater === "function" ? updater(view.columnVisibility) : updater,
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
      columnFilters,
    },
    getRowId,
    enableRowSelection: true,
    onSortingChange,
    onColumnVisibilityChange,
    onRowSelectionChange: setRowSelection,
    onGlobalFilterChange: setGlobalFilter,
    onColumnFiltersChange: setColumnFilters,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getFacetedRowModel: getFacetedRowModel(),
    getFacetedUniqueValues: getFacetedUniqueValues(),
  });

  const hideable = table.getAllColumns().filter((c) => c.getCanHide());
  const rows = table.getRowModel().rows;
  const selectedCount = table.getSelectedRowModel().rows.length;
  const filtersActive = columnFilters.length > 0 || globalFilter.length > 0;

  // Infinite scroll: observe a sentinel at the bottom of the scroll area.
  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !onLoadMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting && hasMore && !isFetchingMore) onLoadMore();
      },
      { root: scrollRef.current, rootMargin: "300px" },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [onLoadMore, hasMore, isFetchingMore]);

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

  const colCount = table.getVisibleLeafColumns().length;

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

        {facets.map((facet) => {
          const column = table.getColumn(facet.columnId);
          return column ? <FacetFilter key={facet.columnId} column={column} label={facet.label} /> : null;
        })}
        {filtersActive && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setColumnFilters([]);
              setGlobalFilter("");
            }}
          >
            <X /> Reset
          </Button>
        )}

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

      {/* table (scroll container owns infinite scroll) */}
      <div ref={scrollRef} className="min-h-0 flex-1 overflow-auto rounded-lg border">
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
                <TableCell colSpan={colCount} className="h-40 text-center text-sm text-destructive">
                  {error}
                </TableCell>
              </TableRow>
            ) : rows.length === 0 ? (
              <TableRow className="hover:bg-transparent">
                <TableCell colSpan={colCount} className="h-40 text-center text-sm text-muted-foreground">
                  {filtersActive ? "No matches." : emptyMessage}
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
        {/* infinite-scroll sentinel + loading indicator */}
        {!isLoading && !error && rows.length > 0 && (
          <div ref={sentinelRef} className="flex items-center justify-center py-3 text-xs text-muted-foreground">
            {isFetchingMore ? (
              <span className="flex items-center gap-1.5">
                <Loader2 className="size-3.5 animate-spin" /> Loading more…
              </span>
            ) : hasMore ? (
              <span>Scroll for more</span>
            ) : null}
          </div>
        )}
      </div>

      {/* footer */}
      <div className="flex shrink-0 items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>
          {rows.length} row{rows.length === 1 ? "" : "s"}
          {filtersActive ? " (filtered)" : ""}
        </span>
        {selectedCount > 0 && <span>{selectedCount} selected</span>}
      </div>
    </div>
  );
}

function FacetFilter<TData>({ column, label }: { column: Column<TData, unknown>; label: string }) {
  const selected = new Set((column.getFilterValue() as string[]) ?? []);
  const options = [...column.getFacetedUniqueValues().entries()]
    .filter(([value]) => value != null && value !== "")
    .sort((a, b) => String(a[0]).localeCompare(String(b[0])));

  const toggle = (value: string) => {
    const next = new Set(selected);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    column.setFilterValue(next.size ? [...next] : undefined);
  };

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5 border-dashed capitalize">
          <Filter className="size-3.5" /> {label}
          {selected.size > 0 && (
            <Badge variant="secondary" className="ml-1 rounded px-1 font-normal">
              {selected.size}
            </Badge>
          )}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-56 p-1.5">
        <div className="flex max-h-72 flex-col overflow-auto">
          {options.length === 0 ? (
            <p className="px-2 py-1.5 text-xs text-muted-foreground">No values</p>
          ) : (
            options.map(([value, count]) => {
              const v = String(value);
              return (
                <label
                  key={v}
                  className="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm capitalize hover:bg-muted"
                >
                  <input
                    type="checkbox"
                    className="accent-primary"
                    checked={selected.has(v)}
                    onChange={() => toggle(v)}
                  />
                  <span className="flex-1 truncate">{v}</span>
                  <span className="text-xs text-muted-foreground">{count}</span>
                </label>
              );
            })
          )}
          {selected.size > 0 && (
            <button
              type="button"
              onClick={() => column.setFilterValue(undefined)}
              className="mt-1 border-t px-2 py-1.5 text-left text-xs text-muted-foreground hover:text-foreground"
            >
              Clear
            </button>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
