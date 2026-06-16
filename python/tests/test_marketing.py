"""Marketing service — ASGI tests against a fake bus (no real NATS, no network).

Drives the public app the way a browser does: loads the page, then fires the three funnel
beacons. Asserts each one publishes the right acquisition.* event (tenant_id=public) and that
an invalid email is rejected without publishing. Mirrors the integration harness's pattern of
injecting app.state directly (httpx ASGITransport doesn't run lifespan).
"""

from __future__ import annotations

import asyncio
import json

import httpx

from maat.marketing import app as mkt


class FakeNats:
    """Captures (subject, decoded-envelope) for each publish; satisfies the publish/flush API."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject: str, payload: bytes) -> None:
        self.published.append((subject, json.loads(payload)))

    async def flush(self) -> None:
        pass

    async def close(self) -> None:
        pass


def _events(bus: FakeNats, type_: str) -> list[dict]:
    return [env for _, env in bus.published if env["type"] == type_]


async def _run() -> None:
    bus = FakeNats()
    mkt.app.state.nats = bus
    transport = httpx.ASGITransport(app=mkt.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mkt") as ac:
        page = await ac.get("/")
        assert page.status_code == 200
        assert "weighted by truth" in page.text  # the headline rendered
        assert "Download on the" in page.text  # the App Store CTA rendered

        assert (await ac.get("/healthz")).json() == {"ok": True}

        assert (await ac.post("/track/view", json={"path": "/", "visitor": "v1"})).status_code == 200

        click = await ac.post("/track/click", json={"platform": "MAC", "visitor": "v1"})
        assert click.json()["message"] == "Coming soon"

        ok = await ac.post("/notify", json={"email": "Reader@Example.com ", "visitor": "v1"})
        assert ok.status_code == 200 and ok.json()["ok"] is True

        bad = await ac.post("/notify", json={"email": "nope"})
        assert bad.status_code == 422  # invalid email rejected

    # Each beacon published exactly the event we expect, all on the public tenant.
    assert _events(bus, "acquisition.page_viewed")
    assert all(env["tenant_id"] == "public" for _, env in bus.published)

    clicks = _events(bus, "acquisition.cta_clicked")
    assert len(clicks) == 1 and clicks[0]["data"]["platform"] == "mac"  # normalized

    notifies = _events(bus, "acquisition.notify_requested")
    assert len(notifies) == 1  # the invalid email did NOT publish
    assert notifies[0]["data"]["email"] == "reader@example.com"  # trimmed + lowercased


def test_marketing_app_serves_page_and_publishes_funnel_events():
    asyncio.run(_run())
