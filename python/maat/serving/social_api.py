"""Comments + pins HTTP API (#49, P5) — client-facing routes over the event-sourced social layer.

Mutations publish ``comment.added`` / ``comment.deleted`` / ``story.pinned`` / ``story.unpinned`` to
the bus (the kernel is the single writer that records them to the append-only log); reads fold those
events at request time via ``serving.social`` — the SAME read-time-fold pattern as the admin events
(source flags, geo, config), so no kernel projection/migration is needed.

Scope (D23 override — cauri wants cross-device comments/pins): comments share a per-TENANT thread;
pins are per ``user_id``. Per-user auth (#51) is deferred, so ``user_id`` is a client-supplied
author/device id and the tenant defaults to the single live tenant. Mounted on ``/api/v2`` next to
the feed router (the Apple client's API).
"""

from __future__ import annotations

import json
from typing import Any

try:
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
except Exception:  # pragma: no cover - FastAPI optional at import time (mirrors feed.py)
    APIRouter = HTTPException = Request = JSONResponse = BaseModel = None  # type: ignore[assignment,misc]

from maat.events import publish
from maat.serving import social

# Per-user auth (#51) is deferred; comments/pins scope to the single live tenant for now.
DEFAULT_TENANT = "cauri"

_COMMENT_TYPES = (social.COMMENT_ADDED, social.COMMENT_DELETED)
_PIN_TYPES = (social.STORY_PINNED, social.STORY_UNPINNED)


if BaseModel is not None:

    class CommentReq(BaseModel):
        cluster_id: str
        user_id: str
        body: str
        tenant_id: str | None = None

    class PinReq(BaseModel):
        cluster_id: str
        user_id: str
        tenant_id: str | None = None


async def _load_social_events(pool: Any, types: tuple[str, ...]) -> list[dict[str, Any]]:
    """Fetch the social event stream (oldest-first) for ``types``, shaped for the social folds."""
    rows = await pool.fetch(
        "select type, data, tenant_id, created_at from events "
        "where type = any($1::text[]) order by id",
        list(types),
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        data = r["data"]
        out.append(
            {
                "type": r["type"],
                "data": json.loads(data) if isinstance(data, str) else (data or {}),
                "tenant_id": r["tenant_id"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    return out


async def _publish_event(request: Any, payload: dict[str, Any], *, stream_id: str) -> None:
    """Publish a social event payload to the bus (the kernel records it). 503 if the bus is down."""
    nc = getattr(request.app.state, "nats", None)
    if nc is None:
        raise HTTPException(status_code=503, detail="event bus unavailable")
    await publish(nc, payload["type"], stream_id, payload["data"], payload["tenant_id"])
    await nc.flush()


def make_social_router() -> Any:
    """Build the comments + pins APIRouter (mounted on /api/v2 next to the feed router)."""
    router = APIRouter(prefix="/api/v2", tags=["social-v2"])

    @router.post("/comments", response_class=JSONResponse)
    async def add_comment(req: "CommentReq", request: Request):
        """#49 — add a comment to a story cluster (tenant-scoped, event-sourced)."""
        body = (req.body or "").strip()
        if not body:
            raise HTTPException(status_code=400, detail="comment body is empty")
        payload = social.make_comment_added(
            tenant_id=req.tenant_id or DEFAULT_TENANT,
            user_id=req.user_id,
            cluster_id=req.cluster_id,
            body=body,
        )
        await _publish_event(request, payload, stream_id=req.cluster_id)
        return JSONResponse(payload["data"], status_code=201)

    @router.delete("/comments/{comment_id}", response_class=JSONResponse)
    async def delete_comment(comment_id: str, cluster_id: str, user_id: str, request: Request):
        """#49 — soft-delete a comment. Author/admin policy is the caller's to enforce."""
        payload = social.make_comment_deleted(
            tenant_id=DEFAULT_TENANT, user_id=user_id, cluster_id=cluster_id, comment_id=comment_id
        )
        await _publish_event(request, payload, stream_id=cluster_id)
        return JSONResponse({"comment_id": comment_id, "deleted": True})

    @router.get("/comments/{cluster_id}", response_class=JSONResponse)
    async def list_comments(cluster_id: str, request: Request, tenant_id: str = DEFAULT_TENANT):
        """#49 — the current comment thread for a story (oldest-first; deletes removed)."""
        events = await _load_social_events(request.app.state.pool, _COMMENT_TYPES)
        thread = social.build_comments(events, tenant_id=tenant_id, cluster_id=cluster_id)
        return JSONResponse({"cluster_id": cluster_id, "comments": thread})

    @router.post("/pins", response_class=JSONResponse)
    async def add_pin(req: "PinReq", request: Request):
        """#49 — pin a story to a user's reading list."""
        payload = social.make_story_pinned(
            tenant_id=req.tenant_id or DEFAULT_TENANT, user_id=req.user_id, cluster_id=req.cluster_id
        )
        await _publish_event(request, payload, stream_id=req.user_id)
        return JSONResponse({"cluster_id": req.cluster_id, "pinned": True}, status_code=201)

    @router.delete("/pins/{cluster_id}", response_class=JSONResponse)
    async def remove_pin(cluster_id: str, user_id: str, request: Request):
        """#49 — unpin a story from a user's reading list."""
        payload = social.make_story_unpinned(
            tenant_id=DEFAULT_TENANT, user_id=user_id, cluster_id=cluster_id
        )
        await _publish_event(request, payload, stream_id=user_id)
        return JSONResponse({"cluster_id": cluster_id, "pinned": False})

    @router.get("/pins", response_class=JSONResponse)
    async def list_pins(request: Request, user_id: str, tenant_id: str = DEFAULT_TENANT):
        """#49 — a user's pinned cluster_ids (most recently pinned first)."""
        events = await _load_social_events(request.app.state.pool, _PIN_TYPES)
        pins = social.build_user_pins(events, tenant_id=tenant_id, user_id=user_id)
        return JSONResponse({"user_id": user_id, "pins": pins})

    return router


social_router = make_social_router() if APIRouter is not None else None
