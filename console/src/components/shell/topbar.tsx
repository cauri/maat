"use client";

import { usePathname } from "next/navigation";

import { ScrollText, Search, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { roomForPath } from "@/lib/rooms";
import { cn } from "@/lib/utils";

import { LiveStatus } from "./live-status";
import { useShell } from "./shell-context";
import { ThemeToggle } from "./theme-toggle";
import { UserMenu } from "./user-menu";

export function Topbar() {
  const pathname = usePathname();
  const room = roomForPath(pathname);
  const { palette, audit, sia } = useShell();

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b bg-background/80 px-3 backdrop-blur sm:px-4">
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        {room ? (
          <>
            <room.icon className="size-4.5 shrink-0 text-muted-foreground" />
            <div className="flex min-w-0 flex-col leading-tight">
              <h1 className="truncate text-sm font-semibold tracking-tight">{room.title}</h1>
              <p className="hidden truncate text-xs text-muted-foreground sm:block">{room.blurb}</p>
            </div>
          </>
        ) : (
          <h1 className="truncate text-sm font-semibold tracking-tight">Maat operator console</h1>
        )}
      </div>

      <button
        type="button"
        onClick={() => palette.set(true)}
        className={cn(
          "hidden h-8 items-center gap-2 rounded-md border bg-muted/40 px-2.5 text-sm text-muted-foreground outline-none transition-colors md:flex",
          "hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring",
        )}
      >
        <Search className="size-4" />
        <span className="hidden lg:inline">Search or jump to…</span>
        <kbd className="ml-1 rounded border bg-background px-1.5 font-mono text-[0.625rem] text-muted-foreground">
          ⌘K
        </kbd>
      </button>

      <div className="flex items-center gap-1.5">
        <LiveStatus className="hidden sm:inline-flex" />

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              className="md:hidden"
              onClick={() => palette.set(true)}
              aria-label="Command palette"
            >
              <Search />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Search (⌘K)</TooltipContent>
        </Tooltip>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={audit.toggle}
              aria-pressed={audit.open}
              aria-label="Audit log"
            >
              <ScrollText />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Audit log</TooltipContent>
        </Tooltip>

        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              onClick={sia.toggle}
              aria-pressed={sia.open}
              className="gap-1.5"
            >
              <Sparkles className="text-primary" />
              <span className="hidden sm:inline">Sia</span>
            </Button>
          </TooltipTrigger>
          <TooltipContent>Your collaborator</TooltipContent>
        </Tooltip>

        <Separator orientation="vertical" className="mx-0.5 data-[orientation=vertical]:h-5" />
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}
