/**
 * Verify the shared `maat_admin` session cookie — a faithful TypeScript port of
 * `verify_cookie` in `python/maat/serving/admin_auth.py`.
 *
 * Format (set by the FastAPI gate): `<payload_b64url>.<sig_b64url>` where
 *   sig = HMAC-SHA256(secret, payload_b64url)   // signed over the base64url *string*
 *   payload = base64url(json({sub, email, iat, exp}))
 *
 * Pure Web Crypto + `atob`/`btoa`, so the exact same function runs in both the Edge
 * middleware and Node server components. No DB, no network — stateless, like the
 * Python side (the gate keeps working even if Postgres/NATS are down).
 */

export interface AdminClaims {
  sub?: string;
  email?: string;
  iat?: number;
  exp?: number;
  [key: string]: unknown;
}

function base64urlToBytes(value: string): Uint8Array {
  const pad = value.length % 4 === 0 ? "" : "=".repeat(4 - (value.length % 4));
  const b64 = value.replace(/-/g, "+").replace(/_/g, "/") + pad;
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function bytesToBase64url(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Constant-time string comparison (mirrors `hmac.compare_digest`). */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/**
 * Return the claims iff the signature is valid and `exp` (if present) is in the
 * future; otherwise `null`. `now` is injectable for tests (seconds since epoch).
 */
export async function verifyAdminToken(
  token: string,
  secret: string,
  now?: number,
): Promise<AdminClaims | null> {
  if (!token || !secret) return null;
  // Exactly one "." — same shape check as the Python `token.count(".") != 1`.
  const first = token.indexOf(".");
  if (first <= 0 || first !== token.lastIndexOf(".") || first === token.length - 1) {
    return null;
  }
  const body = token.slice(0, first);
  const sig = token.slice(first + 1);

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const expectedBuf = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  const expected = bytesToBase64url(expectedBuf);
  if (!timingSafeEqual(sig, expected)) return null;

  let claims: AdminClaims;
  try {
    claims = JSON.parse(new TextDecoder().decode(base64urlToBytes(body))) as AdminClaims;
  } catch {
    return null;
  }
  const exp = Number(claims.exp ?? 0);
  const ts = now ?? Math.floor(Date.now() / 1000);
  if (exp && exp <= ts) return null;
  return claims;
}
