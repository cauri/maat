"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { useEventStream } from "@/hooks/use-event-stream";
import type { AdminClaims } from "@/lib/admin-token";
import { consoleSseUrl } from "@/lib/sse";
import { RAIL_COOKIE } from "@/lib/ui-prefs";

import { AuditDrawer } from "./audit-drawer";
import { CommandPalette } from "./command-palette";
import { Rail } from "./rail";
import { ShellContext, type ShellState } from "./shell-context";
import { SiaDock } from "./sia-dock";
import { Topbar } from "./topbar";

/**
 * The application shell: the rail, the topbar, the content area, and the cross-cutting
 * surfaces (⌘K palette, Audit drawer, Sia dock). Owns the single live event-stream
 * connection and the shell UI state, exposed to descendants via {@link ShellContext}.
 *
 * `initialRailCollapsed` comes from a cookie read on the server, so the rail renders at
 * the right width on first paint.
 */
export function AppShell({
  user,
  authEnabled,
  logoutPath,
  initialRailCollapsed,
  children,
}: {
  user: AdminClaims | null;
  authEnabled: boolean;
  logoutPath: string;
  initialRailCollapsed: boolean;
  children: React.ReactNode;
}) {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [auditOpen, setAuditOpen] = useState(false);
  const [siaOpen, setSiaOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(initialRailCollapsed);

  const setRail = useCallback((value: boolean) => {
    setCollapsed(value);
    try {
      document.cookie = `${RAIL_COOKIE}=${value ? "1" : "0"}; path=/; max-age=31536000; samesite=lax`;
    } catch {
      // ignore — cookies disabled
    }
  }, []);

  const sseUrl = useMemo(() => consoleSseUrl(), []);
  const stream = useEventStream(sseUrl);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const value = useMemo<ShellState>(
    () => ({
      user,
      authEnabled,
      logoutPath,
      palette: {
        open: paletteOpen,
        set: setPaletteOpen,
        toggle: () => setPaletteOpen((open) => !open),
      },
      audit: { open: auditOpen, set: setAuditOpen, toggle: () => setAuditOpen((open) => !open) },
      sia: { open: siaOpen, set: setSiaOpen, toggle: () => setSiaOpen((open) => !open) },
      rail: { collapsed, set: setRail, toggle: () => setRail(!collapsed) },
      stream,
    }),
    [user, authEnabled, logoutPath, paletteOpen, auditOpen, siaOpen, collapsed, setRail, stream],
  );

  return (
    <ShellContext.Provider value={value}>
      <div className="flex h-svh w-full overflow-hidden">
        <Rail />
        <div className="flex min-w-0 flex-1 flex-col">
          <Topbar />
          <main className="min-h-0 flex-1 overflow-auto">{children}</main>
        </div>
      </div>
      <CommandPalette />
      <AuditDrawer />
      <SiaDock />
    </ShellContext.Provider>
  );
}
