import { cookies } from "next/headers";

import { AppShell } from "@/components/shell/app-shell";
import { getAdminSession } from "@/lib/admin-session";
import { ADMIN_AUTH_ENABLED, ADMIN_LOGOUT_PATH } from "@/lib/config";
import { RAIL_COOKIE } from "@/lib/ui-prefs";

/**
 * Server layout for every room: resolves the operator's identity from the shared admin
 * cookie (the Edge middleware has already gated the request) and hands it to the client
 * shell. When the gate is disabled (dev) `user` is null and the shell shows a local
 * operator — exactly like the Python side falling open.
 */
export default async function ConsoleLayout({ children }: { children: React.ReactNode }) {
  const [user, jar] = await Promise.all([getAdminSession(), cookies()]);
  const initialRailCollapsed = jar.get(RAIL_COOKIE)?.value === "1";

  return (
    <AppShell
      user={user}
      authEnabled={ADMIN_AUTH_ENABLED}
      logoutPath={ADMIN_LOGOUT_PATH}
      initialRailCollapsed={initialRailCollapsed}
    >
      {children}
    </AppShell>
  );
}
