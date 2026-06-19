import { ArrowUpRight } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { ROOMS } from "@/lib/rooms";

/**
 * The consistent "scaffolded, not yet built" state for a room. The #303 foundation
 * makes every room reachable and on-brand; each subsequent issue replaces this with the
 * real room. Reads everything from the room registry so there's one source of truth.
 */
export function RoomPlaceholder({ id }: { id: string }) {
  const room = ROOMS.find((entry) => entry.id === id);
  if (!room) return null;
  const Icon = room.icon;
  const issueUrl = `https://github.com/cauri/maat/issues/${room.issue}`;

  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="flex max-w-md flex-col items-center gap-4 text-center">
        <div className="flex size-14 items-center justify-center rounded-xl border bg-muted/40">
          <Icon className="size-7 text-muted-foreground" />
        </div>
        <div className="flex flex-col gap-1.5">
          <h2 className="text-xl font-semibold tracking-tight">{room.title}</h2>
          <p className="text-sm text-muted-foreground">{room.blurb}</p>
        </div>
        <Badge variant="secondary">Planned in #{room.issue}</Badge>
        <p className="text-xs text-muted-foreground">
          The shell is live — navigate with the rail or{" "}
          <kbd className="rounded border bg-muted px-1 font-mono text-[0.625rem]">⌘K</kbd>. This room
          arrives in{" "}
          <a
            href={issueUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-0.5 font-medium text-foreground underline-offset-4 hover:underline"
          >
            #{room.issue}
            <ArrowUpRight className="size-3" />
          </a>
          .
        </p>
      </div>
    </div>
  );
}
