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
        assert 'id="beta"' in page.text  # the beta opt-in checkbox is present
        assert "beta tester" in page.text
        assert 'href="/privacy"' in page.text  # consent line links the privacy policy

        # GDPR legal pages serve and carry the substance (cookieless, GDPR, rights).
        privacy = await ac.get("/privacy")
        assert privacy.status_code == 200 and "Privacy Policy" in privacy.text
        assert "GDPR" in privacy.text and "no cookies" in privacy.text
        imprint = await ac.get("/imprint")
        assert imprint.status_code == 200 and "Legal notice" in imprint.text

        assert (await ac.get("/healthz")).json() == {"ok": True}

        assert (await ac.post("/track/view", json={"path": "/", "visitor": "v1"})).status_code == 200

        click = await ac.post("/track/click", json={"platform": "MAC", "visitor": "v1"})
        assert click.json()["message"] == "Coming soon"

        # Beta is an explicit opt-in: it rides the notify event only when the visitor ticked it.
        ok = await ac.post(
            "/notify", json={"email": "Reader@Example.com ", "visitor": "v1", "beta": True}
        )
        assert ok.status_code == 200 and ok.json()["ok"] is True

        plain = await ac.post("/notify", json={"email": "lurker@example.com"})
        assert plain.status_code == 200 and plain.json()["ok"] is True

        bad = await ac.post("/notify", json={"email": "nope"})
        assert bad.status_code == 422  # invalid email rejected

    # Each beacon published exactly the event we expect, all on the public tenant.
    assert _events(bus, "acquisition.page_viewed")
    assert all(env["tenant_id"] == "public" for _, env in bus.published)

    clicks = _events(bus, "acquisition.cta_clicked")
    assert len(clicks) == 1 and clicks[0]["data"]["platform"] == "mac"  # normalized

    notifies = _events(bus, "acquisition.notify_requested")
    assert len(notifies) == 2  # both valid emails published; the invalid one did NOT
    by_email = {n["data"]["email"]: n["data"] for n in notifies}
    assert by_email["reader@example.com"]["beta"] is True  # trimmed + lowercased, opted in
    assert by_email["lurker@example.com"]["beta"] is False  # opt-in defaults off


def test_marketing_app_serves_page_and_publishes_funnel_events():
    asyncio.run(_run())
