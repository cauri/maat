import { NextResponse, type NextRequest } from "next/server";

import { verifyAdminToken } from "@/lib/admin-token";

/**
 * The admin gate, at the edge (Next 16 "proxy" convention) — reuses the shared
 * `maat_admin` cookie (D31/D32).
 *
 * Read directly from `process.env` (not the `@/lib/config` re-exports) so this stays a
 * single self-contained Edge module. When `MAAT_ADMIN_SESSION_SECRET` is unset the gate
 * falls open, mirroring the Python `_admin_gate` middleware in dev/local/test.
 *
 * The OIDC login dance (`/admin/login|callback|logout`) is owned by the FastAPI app;
 * after the #292 cutover Caddy routes those paths there, and they're excluded below so
 * we never trap the round-trip.
 */
const COOKIE = process.env.MAAT_ADMIN_COOKIE_NAME ?? "maat_admin";
const SECRET = process.env.MAAT_ADMIN_SESSION_SECRET ?? "";
const LOGIN = process.env.MAAT_ADMIN_LOGIN_PATH ?? "/admin/login";

export async function proxy(req: NextRequest): Promise<NextResponse> {
  if (!SECRET) return NextResponse.next(); // gate open — dev parity with the Python side

  const token = req.cookies.get(COOKIE)?.value ?? "";
  const claims = await verifyAdminToken(token, SECRET);
  if (claims) return NextResponse.next();

  const url = new URL(LOGIN, req.url);
  url.searchParams.set("next", req.nextUrl.pathname + req.nextUrl.search);
  return NextResponse.redirect(url, 303);
}

export const config = {
  // Gate every page; skip Next internals, static assets, the health probe, and the
  // FastAPI-owned login dance.
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|healthz|admin/login|admin/callback|admin/logout|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|txt)$).*)",
  ],
};
