"use client";

import { PanelLeftOpen, PanelRightClose, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/**
 * The docked inspector panel that the Stories / Claims / Sources rooms open beside their table.
 *
 * Unlike a Sheet, this stays *in* the page layout — the table keeps its context next to it (no
 * dimming, no blur) — and it can be collapsed to a thin rail to reclaim room without losing the
 * selection. Closing it deselects. State (`collapsed`) lives in the parent workspace so a fresh
 * selection (keyed remount) re-expands it.
 */
export function WorkspacePanel({
  open,
  collapsed,
  onCollapsedChange,
  onClose,
  title,
  subtitle,
  collapsedLabel,
  className,
  children,
}: {
  open: boolean;
  collapsed: boolean;
  onCollapsedChange: (v: boolean) => void;
  onClose: () => void;
  title: React.ReactNode;
  subtitle?: React.ReactNode;
  /** Short text shown vertically on the collapsed rail. */
  collapsedLabel?: string;
  /** Width override for the expanded panel (defaults to a sensible inspector width). */
  className?: string;
  children: React.ReactNode;
}) {
  if (!open) return null;

  if (collapsed) {
    return (
      <aside className="flex h-full w-10 shrink-0 flex-col items-center gap-2 rounded-lg border bg-card py-2">
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => onCollapsedChange(false)}
          aria-label="Expand panel"
        >
          <PanelLeftOpen />
        </Button>
        {collapsedLabel && (
          <span className="flex-1 select-none overflow-hidden whitespace-nowrap text-xs text-muted-foreground [writing-mode:vertical-rl]">
            {collapsedLabel}
          </span>
        )}
        <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close panel">
          <X />
        </Button>
      </aside>
    );
  }

  return (
    <aside
      className={cn(
        "flex h-full w-full shrink-0 flex-col overflow-hidden rounded-lg border bg-card sm:w-[30rem]",
        className,
      )}
    >
      <div className="flex shrink-0 items-start gap-2 border-b p-3">
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="text-base font-semibold leading-snug">{title}</div>
          {subtitle && (
            <div className="flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
              {subtitle}
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center">
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => onCollapsedChange(true)}
            aria-label="Collapse panel"
          >
            <PanelRightClose />
          </Button>
          <Button variant="ghost" size="icon-sm" onClick={onClose} aria-label="Close panel">
            <X />
          </Button>
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">{children}</div>
    </aside>
  );
}
