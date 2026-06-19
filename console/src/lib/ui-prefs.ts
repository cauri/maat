/**
 * Client-safe UI preference keys (no env, importable from both server and client).
 * Rail collapse is stored in a cookie so the server renders the correct width on first
 * paint — no flash, no hydration mismatch.
 */
export const RAIL_COOKIE = "maat_console_rail";

/** Sia dock open/closed, persisted so the server renders the right layout (default: open). */
export const SIA_COOKIE = "maat_console_sia";
