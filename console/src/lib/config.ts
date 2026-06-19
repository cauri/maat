/**
 * Server-side configuration for the operator console.
 *
 * The console **reuses the existing admin gate** (D31/D32): the FastAPI app
 * (`python/maat/serving/admin_auth.py`) owns the Google-OIDC dance and issues a
 * stateless, HMAC-signed `maat_admin` session cookie. This app verifies the *same*
 * cookie with the *same* `MAAT_ADMIN_SESSION_SECRET` — there is no second identity
 * system. When the secret is unset the gate falls open, exactly as the Python side
 * does, so local/dev/test behave the same way.
 *
 * These names mirror the Python config (`admin_auth.load_config`) so one set of
 * environment variables drives both processes behind `admin.maat.press`.
 */

/** Cookie the FastAPI gate sets (`admin_auth.SESSION_COOKIE`). */
export const ADMIN_COOKIE_NAME = process.env.MAAT_ADMIN_COOKIE_NAME ?? "maat_admin";

/** Shared HMAC-SHA256 secret (`MAAT_ADMIN_SESSION_SECRET`). Empty ⇒ gate open. */
export const ADMIN_SESSION_SECRET = process.env.MAAT_ADMIN_SESSION_SECRET ?? "";

/** The gate is live only when a secret is configured — matches `AdminConfig.enabled`. */
export const ADMIN_AUTH_ENABLED = ADMIN_SESSION_SECRET.length > 0;

/** Where to send unauthenticated callers — the FastAPI login route (D32). */
export const ADMIN_LOGIN_PATH = process.env.MAAT_ADMIN_LOGIN_PATH ?? "/admin/login";

/** Sign-out route, also owned by FastAPI (clears the shared cookie). */
export const ADMIN_LOGOUT_PATH = process.env.MAAT_ADMIN_LOGOUT_PATH ?? "/admin/logout";
