import { NextResponse } from "next/server";

/**
 * Liveness probe for the container orchestrator / Caddy. Deliberately open (excluded
 * from the admin gate in `middleware.ts`) so health checks don't 303 to the login page.
 */
export function GET() {
  return NextResponse.json({ ok: true, service: "maat-console" });
}
