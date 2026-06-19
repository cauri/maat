"""#297 — extract idempotency: an at-least-once redelivery must not re-extract / double-publish.

Every other live stage is idempotent at the kernel fold (claims.classified UPDATEs by claim id;
cluster.corroborated / claim.related upsert by their deterministic ids). Extract is the exception —
claim ids are content-random, so re-running the LLM would append a second set. The handler guards
against that by skipping when the article already has claims; these tests pin that behaviour.
"""

import asyncio

import maat.agents.extract_agent as ex

EVENT = {
    "stream_id": "gd-abc123",
    "data": {"title": "Headline", "body": "A real article body.", "source": "x.com", "language": "en"},
}


class _Pool:
    """Fake asyncpg pool whose claims-existence check is fixed by the test."""

    def __init__(self, has_claims: bool):
        self._has = has_claims

    async def fetchval(self, *_a, **_k):
        return 1 if self._has else None


def _patch(monkeypatch) -> list[dict]:
    published: list[dict] = []

    async def fake_publish(nc, type_, stream_id, data, tenant_id="cauri"):
        published.append({"type": type_, "stream_id": stream_id, "data": data})

    monkeypatch.setattr(ex, "is_index_page", lambda title, body: False)  # not an index page
    monkeypatch.setattr(ex, "publish", fake_publish)
    return published


def test_redelivery_skips_when_article_already_has_claims(monkeypatch):
    published = _patch(monkeypatch)
    llm_calls: list[int] = []
    monkeypatch.setattr(ex, "extract_claims", lambda *a, **k: llm_calls.append(1) or [])
    monkeypatch.setattr(ex, "_pool", _Pool(has_claims=True))

    asyncio.run(ex.handle(nc=None, event=EVENT))

    assert not llm_calls, "extract_claims (the LLM) must NOT run again on a redelivery"
    assert not published, "no second claims.extracted may be published on a redelivery"


def test_first_delivery_extracts_and_publishes_once(monkeypatch):
    published = _patch(monkeypatch)

    class _Claim:
        def model_dump(self):
            return {"text": "c", "voice": "own", "evidence_span": "e"}

    monkeypatch.setattr(ex, "extract_claims", lambda *a, **k: [_Claim()])

    async def fake_active_text(pool, name, default):
        return "PROMPT"

    monkeypatch.setattr(ex.prompts, "active_text", fake_active_text)
    monkeypatch.setattr(ex, "_pool", _Pool(has_claims=False))

    asyncio.run(ex.handle(nc=None, event=EVENT))

    assert len(published) == 1
    assert published[0]["type"] == "claims.extracted"
    assert published[0]["stream_id"] == "gd-abc123"
    assert published[0]["data"]["article_id"] == "gd-abc123"
    assert len(published[0]["data"]["claims"]) == 1
