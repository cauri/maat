"""Inbound hardening for the open public API (#280): a per-IP rate limiter and a request body-size
cap, as dependency-free ASGI middleware.

`/api/translate` and `/api/feedback` are the only unauthenticated, state-affecting endpoints on the
public surface (the admin gate deliberately lets `/api/*` through). The audit flagged three open
exposures — unbounded request bodies (memory-exhaustion DoS), no per-IP rate limit (one client can
flood the `events` log / `/review` queue or burn the translate provider budget). The tight per-field
caps live on the Pydantic models (a `max_length` → 422); this module is the coarse, transport-level
guard: oversized bodies → 413, per-IP floods → 429.

Why custom rather than `slowapi`: the box is a single process, the limits are simple, and we already
model rate limiting as an injectable-clock token bucket (`providers/seam.py`, #300). Keeping it
in-house means no new dependency, deterministic tests (inject the clock), and one obvious home for
both guards — which is also where the surviving public routes will land when `web/app.py` is retired
(#292). Lives in `serving/` (a neutral home web may import; it never imports web).
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

# ── defaults (overridable from the box env) ──────────────────────────────────────────────────────
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB — well above any legit JSON (NATS caps payloads at 1 MB)
DEFAULT_BURST = 30  # tokens a single IP may spend back-to-back
DEFAULT_REFILL_PER_SEC = 1.0  # sustained rate once the burst drains
DEFAULT_MAX_IPS = 4096  # bound the per-IP table so a spray of source IPs can't grow it without limit

ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]


# ── token bucket (per IP) ────────────────────────────────────────────────────────────────────────


@dataclass
class _TokenBucket:
    """Classic token bucket: `capacity` tokens, refilled at `refill_per_sec`, one spent per request."""

    capacity: float
    refill_per_sec: float
    tokens: float
    updated: float

    def allow(self, now: float, cost: float = 1.0) -> bool:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_sec)
        self.updated = now
        # Epsilon for float refill (same fix as the provider seam #300): a refill that should reach a
        # whole token can land a hair under it (e.g. 1.0*dt == 0.999…).
        if self.tokens >= cost - 1e-9:
            self.tokens -= cost
            return True
        return False


class PerIpRateLimiter:
    """A bounded registry of per-IP token buckets. `monotonic` is injectable for deterministic tests.

    The IP table is an LRU capped at `max_ips`: a flood of distinct source IPs evicts the coldest
    rather than growing memory without bound. Eviction only ever hands an evicted IP a *fresh* full
    bucket later, so it can't be abused to escape one's own limit (that needs that many real IPs).
    """

    def __init__(
        self,
        *,
        capacity: float = DEFAULT_BURST,
        refill_per_sec: float = DEFAULT_REFILL_PER_SEC,
        max_ips: int = DEFAULT_MAX_IPS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.max_ips = max(1, int(max_ips))
        self._monotonic = monotonic
        self._buckets: "OrderedDict[str, _TokenBucket]" = OrderedDict()

    def allow(self, ip: str, now: float | None = None) -> bool:
        now = self._monotonic() if now is None else now
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = _TokenBucket(self.capacity, self.refill_per_sec, self.capacity, now)
            self._buckets[ip] = bucket
            if len(self._buckets) > self.max_ips:
                self._buckets.popitem(last=False)  # evict the coldest IP
        else:
            self._buckets.move_to_end(ip)
        return bucket.allow(now)

    def retry_after(self) -> int:
        """Seconds until ~1 token refills — the `Retry-After` hint on a 429."""
        return max(1, int(round(1.0 / self.refill_per_sec))) if self.refill_per_sec > 0 else 1


# ── client-ip resolution ─────────────────────────────────────────────────────────────────────────


def client_ip(scope: dict) -> str:
    """The real client IP. Behind Caddy (a single trusted proxy) the client is the left-most
    `X-Forwarded-For` entry; fall back to the socket peer when the header is absent (direct/local)."""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for":
            first = value.decode("latin-1").split(",")[0].strip()
            if first:
                return first
    client = scope.get("client")
    return client[0] if client else "unknown"


# ── ASGI helpers ─────────────────────────────────────────────────────────────────────────────────


async def _send_json(
    send: Callable[[dict], Awaitable[None]],
    status: int,
    payload: dict[str, Any],
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _content_length(scope: dict) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


# ── middleware ───────────────────────────────────────────────────────────────────────────────────


class MaxBodySizeMiddleware:
    """Reject request bodies over `max_bytes` with 413 — *before* the app buffers them. A declared
    `Content-Length` is rejected up front; otherwise the body is read up to the cap (handles missing
    / chunked length) and the request is failed the moment it overflows, then replayed to the app."""

    def __init__(self, app: ASGIApp, *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            return await _send_json(send, 413, {"detail": "request entity too large"})

        body = bytearray()
        trailing: dict | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                trailing = message  # e.g. http.disconnect — forward as-is
                break
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                return await _send_json(send, 413, {"detail": "request entity too large"})
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> dict:
            nonlocal replayed
            if trailing is not None and not replayed:
                replayed = True
                return trailing
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class RateLimitMiddleware:
    """Per-IP token-bucket throttle on the configured path prefixes → 429 (+ `Retry-After`). Other
    paths pass straight through, so only the open public endpoints are limited."""

    def __init__(self, app: ASGIApp, *, limiter: PerIpRateLimiter, prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.limiter = limiter
        self.prefixes = prefixes

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Callable[[dict], Awaitable[None]]) -> None:
        if scope.get("type") == "http" and scope.get("path", "").startswith(self.prefixes):
            if not self.limiter.allow(client_ip(scope)):
                return await _send_json(
                    send,
                    429,
                    {"detail": "rate limit exceeded"},
                    extra_headers=[(b"retry-after", str(self.limiter.retry_after()).encode())],
                )
        return await self.app(scope, receive, send)
