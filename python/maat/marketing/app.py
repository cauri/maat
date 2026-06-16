"""Maat marketing site (maat.press) — public FastAPI service.

Serves the landing page and records the visitor funnel as events on the canonical log
(D5/D20): a page view, a "Download on the App Store" tap (the page shows "coming soon"), and
an optional launch-notify email. maat-kerneld folds these into acquisition_signals /
acquisition_signups; the operator console reads them on /acquisition.

This surface is internet-facing and deliberately separate from the console (`maat.web`), which
is behind-the-box and unauthenticated — they share only the event bus and database. Every event
is pre-user, so it carries tenant_id="public". Best-effort: if NATS is down the page still
serves and tracking simply no-ops — we never block a visitor on the bus. There are no
third-party trackers; the only calls the page makes are POSTs back to this same origin.

Run locally: `make marketing` (uvicorn on :8080).
"""

from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from maat import events
from maat.bus import connect as nats_connect
from maat.marketing.legal import IMPRINT, PRIVACY
from maat.marketing.page import PAGE

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        app.state.nats = await nats_connect()
    except Exception as exc:  # noqa: BLE001 - the page must still serve if the bus is down
        app.state.nats = None
        print(f"[marketing] NATS unavailable, tracking disabled: {exc}", flush=True)
    yield
    if app.state.nats is not None:
        await app.state.nats.close()


app = FastAPI(lifespan=lifespan, title="Maat")
app.state.nats = None  # defined up front so handlers are safe before/without lifespan (tests)


# --- helpers (pure, unit-tested) --------------------------------------------------------


def ua_family(ua: str) -> str:
    """Coarse device family from a User-Agent — we keep only this, never the full string."""
    u = (ua or "").lower()
    if "iphone" in u or "ipad" in u or "ios " in u:
        return "ios"
    if "macintosh" in u or "mac os" in u:
        return "mac"
    if "android" in u:
        return "android"
    if "windows" in u:
        return "windows"
    if "linux" in u:
        return "linux"
    return "other"


def norm_platform(p: str) -> str:
    """The CTA reports which store it stands for; anything unexpected falls back to iOS."""
    p = (p or "").strip().lower()
    return p if p in ("ios", "mac") else "ios"


def valid_email(e: str) -> bool:
    return bool(_EMAIL.match(e or "")) and len(e) <= 254


# --- request models (all fields optional so a thin/old client never 422s the beacon) ----


class Signal(BaseModel):
    path: str = "/"
    referrer: str = ""
    utm_source: str = ""
    utm_medium: str = ""
    utm_campaign: str = ""
    visitor: str = ""


class Click(Signal):
    platform: str = "ios"


class Notify(Signal):
    email: str
    platform: str = ""
    beta: bool = False  # explicit opt-in: the launch form's unticked "beta tester" checkbox


def _payload(body: Signal, request: Request) -> dict:
    return {
        "path": body.path or "/",
        "referrer": (body.referrer or "")[:500],
        "utm_source": (body.utm_source or "")[:120],
        "utm_medium": (body.utm_medium or "")[:120],
        "utm_campaign": (body.utm_campaign or "")[:120],
        "visitor": (body.visitor or "")[:64],
        "ua_family": ua_family(request.headers.get("user-agent", "")),
    }


async def _emit(type_: str, data: dict) -> bool:
    """Publish one acquisition event (tenant_id=public). False if the bus is down — never raises."""
    nc = app.state.nats
    if nc is None:
        return False
    try:
        await events.publish(nc, type_, str(uuid.uuid4()), data, tenant_id=events.PUBLIC_TENANT)
        await nc.flush()
        return True
    except Exception as exc:  # noqa: BLE001 - tracking is best-effort; a visitor is never blocked
        print(f"[marketing] publish {type_} failed: {exc}", flush=True)
        return False


# --- routes -----------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return PAGE


@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> str:
    return PRIVACY


@app.get("/imprint", response_class=HTMLResponse)
async def imprint() -> str:
    return IMPRINT


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/track/view")
async def track_view(body: Signal, request: Request) -> dict:
    await _emit(events.ACQUISITION_PAGE_VIEWED, _payload(body, request))
    return {"ok": True}


@app.post("/track/click")
async def track_click(body: Click, request: Request) -> dict:
    data = _payload(body, request)
    data["platform"] = norm_platform(body.platform)
    await _emit(events.ACQUISITION_CTA_CLICKED, data)
    return {"ok": True, "message": "Coming soon"}


@app.post("/notify")
async def notify(body: Notify, request: Request):
    email = (body.email or "").strip().lower()
    if not valid_email(email):
        return JSONResponse({"ok": False, "error": "Please enter a valid email."}, status_code=422)
    data = _payload(body, request)
    data["email"] = email
    data["platform"] = norm_platform(body.platform) if body.platform else None
    data["beta"] = bool(body.beta)
    await _emit(events.ACQUISITION_NOTIFY_REQUESTED, data)
    return {"ok": True, "message": "Thanks — we'll let you know."}
