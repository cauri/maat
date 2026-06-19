"use client";

import { useRouter } from "next/navigation";

import {
  LogOut,
  type LucideIcon,
  Monitor,
  Moon,
  PanelLeft,
  ScrollText,
  Sparkles,
  Sun,
} from "lucide-react";
import { useTheme } from "next-themes";

import {
  CommandDialog,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command";
import { ROOMS } from "@/lib/rooms";

import { useShell } from "./shell-context";

export function CommandPalette() {
  const router = useRouter();
  const { setTheme } = useTheme();
  const { palette, audit, sia, rail, authEnabled, logoutPath } = useShell();

  const run = (action: () => void) => {
    palette.set(false);
    action();
  };

  return (
    <CommandDialog
      open={palette.open}
      onOpenChange={palette.set}
      title="Command palette"
      description="Jump to a room or run an action."
    >
      <CommandInput placeholder="Search rooms and actions…" />
      <CommandList>
        <CommandEmpty>No matches.</CommandEmpty>

        <CommandGroup heading="Go to">
          {ROOMS.map((room) => (
            <CommandItem
              key={room.id}
              value={`${room.title} ${room.blurb}`}
              onSelect={() => run(() => router.push(room.path))}
            >
              <room.icon />
              <span>{room.title}</span>
              <span className="ml-1 truncate text-xs text-muted-foreground">{room.blurb}</span>
            </CommandItem>
          ))}
        </CommandGroup>

        <CommandSeparator />

        <CommandGroup heading="View">
          <PaletteAction icon={ScrollText} label="Toggle audit log" onSelect={() => run(audit.toggle)} />
          <PaletteAction icon={Sparkles} label="Open Sia" onSelect={() => run(() => sia.set(true))} />
          <PaletteAction
            icon={PanelLeft}
            label={rail.collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onSelect={() => run(rail.toggle)}
          />
          <PaletteAction icon={Sun} label="Theme: Light" onSelect={() => run(() => setTheme("light"))} />
          <PaletteAction icon={Moon} label="Theme: Dark" onSelect={() => run(() => setTheme("dark"))} />
          <PaletteAction
            icon={Monitor}
            label="Theme: System"
            onSelect={() => run(() => setTheme("system"))}
          />
        </CommandGroup>

        {authEnabled && (
          <>
            <CommandSeparator />
            <CommandGroup heading="Session">
              <PaletteAction
                icon={LogOut}
                label="Sign out"
                onSelect={() =>
                  run(() => {
                    window.location.href = logoutPath;
                  })
                }
              />
            </CommandGroup>
          </>
        )}
      </CommandList>
    </CommandDialog>
  );
}

function PaletteAction({
  icon: Icon,
  label,
  onSelect,
}: {
  icon: LucideIcon;
  label: string;
  onSelect: () => void;
}) {
  return (
    <CommandItem value={label} onSelect={onSelect}>
      <Icon />
      <span>{label}</span>
    </CommandItem>
  );
}
