"""Route tests for the comments + pins API (#49) — serving/social_api.py.

Exercises the full wiring through a TestClient with a fake bus: a POST publishes the social event,
and a GET folds the published events back via serving.social — proving the read-time-fold path the
Apple client consumes actually works (not just that the pure folds in test_social.py do).

TestClient is used WITHOUT a context manager, so the app lifespan never runs (no real pool/nats);
app.state.pool/nats are patched with fakes (the pattern in test_admin_auth / test_web).
"""

import json

from starlette.testclient import TestClient

from maat.serving import social
from maat.web import app as appmod


class _Bus:
    """Fake NATS that captures published envelopes AND serves them back as `events`-table rows."""

    def __init__(self):
        self.events: list[dict] = []

    async def publish(self, subject, payload):
        self.events.append(json.loads(payload.decode()))

    async def flush(self):
        pass

    async def fetch(self, _q, *args):
        # Mirrors `select type,data,tenant_id,created_at from events where type = any($1)`.
        types = set(args[0]) if args else set()
        return [
            {"type": e["type"], "data": e["data"], "tenant_id": e["tenant_id"], "created_at": None}
            for e in self.events
            if e["type"] in types
        ]


def _client(monkeypatch):
    bus = _Bus()
    monkeypatch.setattr(appmod.app.state, "nats", bus, raising=False)
    monkeypatch.setattr(appmod.app.state, "pool", bus, raising=False)  # fetch() lives on the bus too
    return TestClient(appmod.app, raise_server_exceptions=True), bus


def test_add_comment_publishes_and_lists_back(monkeypatch):
    client, bus = _client(monkeypatch)
    r = client.post("/api/v2/comments", json={"cluster_id": "c1", "user_id": "u1", "body": "hello"})
    assert r.status_code == 201
    cid = r.json()["comment_id"]
    assert any(e["type"] == social.COMMENT_ADDED for e in bus.events)

    g = client.get("/api/v2/comments/c1")
    assert g.status_code == 200
    comments = g.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["comment_id"] == cid
    assert comments[0]["body"] == "hello"
    assert comments[0]["user_id"] == "u1"


def test_delete_comment_removes_it_from_the_thread(monkeypatch):
    client, _ = _client(monkeypatch)
    cid = client.post(
        "/api/v2/comments", json={"cluster_id": "c1", "user_id": "u1", "body": "x"}
    ).json()["comment_id"]
    d = client.delete(f"/api/v2/comments/{cid}", params={"cluster_id": "c1", "user_id": "u1"})
    assert d.status_code == 200
    assert client.get("/api/v2/comments/c1").json()["comments"] == []


def test_empty_comment_body_rejected(monkeypatch):
    client, bus = _client(monkeypatch)
    r = client.post("/api/v2/comments", json={"cluster_id": "c1", "user_id": "u1", "body": "   "})
    assert r.status_code == 400
    assert not bus.events  # nothing published


def test_comments_are_tenant_scoped(monkeypatch):
    client, _ = _client(monkeypatch)
    client.post(
        "/api/v2/comments", json={"cluster_id": "c1", "user_id": "u1", "body": "hi", "tenant_id": "other"}
    )
    # default-tenant reader sees nothing from the 'other' tenant
    assert client.get("/api/v2/comments/c1").json()["comments"] == []
    assert client.get("/api/v2/comments/c1", params={"tenant_id": "other"}).json()["comments"]


def test_pin_unpin_round_trip_most_recent_first(monkeypatch):
    client, _ = _client(monkeypatch)
    client.post("/api/v2/pins", json={"cluster_id": "c1", "user_id": "u1"})
    client.post("/api/v2/pins", json={"cluster_id": "c2", "user_id": "u1"})
    assert client.get("/api/v2/pins", params={"user_id": "u1"}).json()["pins"] == ["c2", "c1"]

    client.delete("/api/v2/pins/c1", params={"user_id": "u1"})
    assert client.get("/api/v2/pins", params={"user_id": "u1"}).json()["pins"] == ["c2"]


def test_pins_are_per_user(monkeypatch):
    client, _ = _client(monkeypatch)
    client.post("/api/v2/pins", json={"cluster_id": "c1", "user_id": "u1"})
    assert client.get("/api/v2/pins", params={"user_id": "u2"}).json()["pins"] == []


def test_bus_down_returns_503(monkeypatch):
    client, _ = _client(monkeypatch)
    monkeypatch.setattr(appmod.app.state, "nats", None, raising=False)
    r = client.post("/api/v2/comments", json={"cluster_id": "c1", "user_id": "u1", "body": "hi"})
    assert r.status_code == 503
