"use client";

import { ScrollText } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { relativeTime } from "@/lib/time";

import { LiveStatus } from "./live-status";
import { useShell } from "./shell-context";

export function AuditDrawer() {
  const { audit, stream } = useShell();
  const events = stream.events;

  return (
    <Sheet open={audit.open} onOpenChange={audit.set}>
      <SheetContent side="right" className="w-full gap-0 p-0 sm:max-w-md">
        <SheetHeader className="border-b">
          <div className="flex items-center justify-between gap-2">
            <SheetTitle className="flex items-center gap-2">
              <ScrollText className="size-4" /> Audit log
            </SheetTitle>
            <LiveStatus />
          </div>
          <SheetDescription>
            Every operator action — yours and Sia&apos;s — read live from the event log (D5).
          </SheetDescription>
        </SheetHeader>

        {events.length === 0 ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-2 px-8 text-center">
            <ScrollText className="size-8 text-muted-foreground/40" />
            <p className="text-sm font-medium">No activity yet</p>
            <p className="max-w-xs text-xs text-muted-foreground">
              Actions appear here the moment they happen, once the command/query API (#304) is
              connected. Every change is a typed, audited event.
            </p>
          </div>
        ) : (
          <ScrollArea className="min-h-0 flex-1">
            <ul className="divide-y">
              {events.map((event) => (
                <li key={event.key} className="flex flex-col gap-1 px-4 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <code className="truncate font-mono text-xs">{event.type}</code>
                    <time className="shrink-0 text-xs text-muted-foreground" dateTime={new Date(event.ts).toISOString()}>
                      {relativeTime(event.ts)}
                    </time>
                  </div>
                  {event.actor && <p className="text-xs text-muted-foreground">by {event.actor}</p>}
                </li>
              ))}
            </ul>
          </ScrollArea>
        )}

        {events.length > 0 && (
          <div className="border-t p-2">
            <Button variant="ghost" size="sm" className="w-full" onClick={stream.clear}>
              Clear view
            </Button>
          </div>
        )}
      </SheetContent>
    </Sheet>
  );
}
