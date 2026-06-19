import "server-only";

import { cookies } from "next/headers";

import { ADMIN_AUTH_ENABLED, ADMIN_COOKIE_NAME, ADMIN_SESSION_SECRET } from "./config";
import { type AdminClaims, verifyAdminToken } from "./admin-token";

/**
 * Read + verify the current operator's admin session from the shared cookie.
 *
 * Returns `null` when the gate is disabled (dev parity with the Python side) or when
 * the cookie is missing/invalid. In production the Edge middleware has already
 * redirected unauthenticated callers, so a Server Component can treat `null` here as
 * "auth disabled" rather than "anonymous".
 */
export async function getAdminSession(): Promise<AdminClaims | null> {
  if (!ADMIN_AUTH_ENABLED) return null;
  const jar = await cookies();
  const token = jar.get(ADMIN_COOKIE_NAME)?.value ?? "";
  return verifyAdminToken(token, ADMIN_SESSION_SECRET);
}

export { ADMIN_AUTH_ENABLED } from "./config";
export type { AdminClaims } from "./admin-token";
