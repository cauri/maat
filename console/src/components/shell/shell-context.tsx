"use client";

import { createContext, useContext } from "react";

import type { ConsoleEvent, StreamStatus } from "@/hooks/use-event-stream";
import type { AdminClaims } from "@/lib/admin-token";

interface Toggle {
  open: boolean;
  set: (value: boolean) => void;
  toggle: () => void;
}

interface RailState {
  collapsed: boolean;
  set: (value: boolean) => void;
  toggle: () => void;
}

export interface ShellState {
  /** The signed-in operator, or null when the gate is disabled (dev). */
  user: AdminClaims | null;
  authEnabled: boolean;
  /** FastAPI sign-out route (clears the shared cookie); threaded from server config. */
  logoutPath: string;
  palette: Toggle;
  audit: Toggle;
  sia: Toggle;
  rail: RailState;
  stream: { status: StreamStatus; events: ConsoleEvent[]; clear: () => void };
}

export const ShellContext = createContext<ShellState | null>(null);

export function useShell(): ShellState {
  const ctx = useContext(ShellContext);
  if (!ctx) throw new Error("useShell must be used within <AppShell>");
  return ctx;
}
