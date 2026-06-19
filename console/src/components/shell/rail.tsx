"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { Feather, PanelLeft, PanelLeftClose } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { ALTITUDE_GROUPS, type Room, roomsByAltitude } from "@/lib/rooms";
import { cn } from "@/lib/utils";

import { useShell } from "./shell-context";

function isActive(pathname: string, room: Room): boolean {
  return pathname === room.path || pathname.startsWith(`${room.path}/`);
}

function RailItem({ room, active, collapsed }: { room: Room; active: boolean; collapsed: boolean }) {
  const Icon = room.icon;
  const link = (
    <Link
      href={room.path}
      aria-current={active ? "page" : undefined}
      className={cn(
        "group flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium outline-none transition-colors",
        "focus-visible:ring-2 focus-visible:ring-sidebar-ring",
        collapsed && "justify-center px-0",
        active
          ? "bg-sidebar-accent text-sidebar-accent-foreground"
          : "text-sidebar-foreground/70 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground",
      )}
    >
      <Icon className="size-4 shrink-0" />
      {!collapsed && <span className="truncate">{room.title}</span>}
    </Link>
  );

  if (!collapsed) return link;
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right" className="flex flex-col gap-0.5">
        <span className="font-medium">{room.title}</span>
        <span className="max-w-50 text-muted-foreground">{room.blurb}</span>
      </TooltipContent>
    </Tooltip>
  );
}

export function Rail() {
  const pathname = usePathname();
  const { rail } = useShell();
  const collapsed = rail.collapsed;

  return (
    <aside
      className={cn(
        "flex h-full shrink-0 flex-col border-r bg-sidebar text-sidebar-foreground transition-[width] duration-200 ease-in-out",
        collapsed ? "w-[3.75rem]" : "w-60",
      )}
    >
      <div className={cn("flex h-14 items-center gap-2.5 px-3", collapsed && "justify-center px-0")}>
        <div className="flex size-8 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
          <Feather className="size-4.5" />
        </div>
        {!collapsed && (
          <div className="flex min-w-0 flex-col leading-tight">
            <span className="truncate text-sm font-semibold tracking-tight">Maat</span>
            <span className="truncate text-xs text-muted-foreground">operator console</span>
          </div>
        )}
      </div>
      <Separator />

      <ScrollArea className="min-h-0 flex-1">
        <nav className="flex flex-col gap-4 px-2 py-3">
          {ALTITUDE_GROUPS.map((group) => {
            const rooms = roomsByAltitude(group.id);
            if (rooms.length === 0) return null;
            return (
              <div key={group.id} className="flex flex-col gap-1">
                {group.label &&
                  (collapsed ? (
                    <Separator className="mx-auto my-1 w-6" />
                  ) : (
                    <p className="px-2.5 pb-0.5 text-[0.6875rem] font-medium uppercase tracking-wider text-muted-foreground/70">
                      {group.label}
                    </p>
                  ))}
                {rooms.map((room) => (
                  <RailItem
                    key={room.id}
                    room={room}
                    active={isActive(pathname, room)}
                    collapsed={collapsed}
                  />
                ))}
              </div>
            );
          })}
        </nav>
      </ScrollArea>

      <Separator />
      <div className={cn("flex p-2", collapsed ? "justify-center" : "justify-end")}>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={rail.toggle}
              aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            >
              {collapsed ? <PanelLeft /> : <PanelLeftClose />}
            </Button>
          </TooltipTrigger>
          <TooltipContent side="right">{collapsed ? "Expand" : "Collapse"}</TooltipContent>
        </Tooltip>
      </div>
    </aside>
  );
}
