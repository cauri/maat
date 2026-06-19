"""#290 — the shared acquisition Ingestor + driver dispatch.

Pure unit tests: publish / fetch / clean / source-gate are stubbed so we assert the dedup → deny →
gate → fetch → clean → publish(article.ingested) policy each source relies on, with no DB/NATS/LLM.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import maat.acquire.ingest as ing_mod
from maat import ids
from maat.acquire.drivers import SOURCES, acquire
from maat.acquire.ingest import AcqState, Ingestor


def _state(seen=None, denied=None) -> AcqState:
    return AcqState(seen=set(seen or []), denied=set(denied or []), gate_prompt="p", known_good=frozenset())


def _patch_publish(monkeypatch) -> list[dict]:
    published: list[dict] = []

    async def fake_publish(nc, type_, stream_id, data, tenant_id="cauri"):
        published.append({"type": type_, "stream_id": stream_id, "data": data})

    monkeypatch.setattr(ing_mod, "publish", fake_publish)
    return published


def _identity_clean(monkeypatch) -> None:
    monkeypatch.setattr(ing_mod, "clean_article", lambda t, b, s: (t, b))


def _ingest(ing: Ingestor, **kw) -> bool:
    base = dict(url="http://x/a", title="T", source="x.com", language="en", body="body text", image=None)
    base.update(kw)
    return asyncio.run(ing.ingest(**base))


def test_publishes_article_ingested_with_content_addressed_id(monkeypatch):
    pub = _patch_publish(monkeypatch)
    _identity_clean(monkeypatch)
    ing = Ingestor(None, _state(), prefix="rss", gate=False)
    assert _ingest(ing, url="http://x/a") is True
    assert pub[0]["type"] == "article.ingested"
    assert pub[0]["stream_id"] == ids.article_id("http://x/a", "rss")  # content-addressed (#289)
    assert ing.new == 1


def test_dedup_skips_seen_url(monkeypatch):
    pub = _patch_publish(monkeypatch)
    _identity_clean(monkeypatch)
    ing = Ingestor(None, _state(), prefix="rss", gate=False)
    assert _ingest(ing, url="http://x/dup") is True
    assert _ingest(ing, url="http://x/dup") is False  # redelivery / re-sighting → no-op
    assert len(pub) == 1


def test_denied_source_is_dropped(monkeypatch):
    pub = _patch_publish(monkeypatch)
    ing = Ingestor(None, _state(denied=["bad.com"]), prefix="rss", gate=False)
    assert _ingest(ing, source="bad.com") is False
    assert ing.dropped == 1 and not pub


def test_source_gate_rejects_and_accepts(monkeypatch):
    pub = _patch_publish(monkeypatch)
    _identity_clean(monkeypatch)
    monkeypatch.setattr(ing_mod, "accept_source", lambda *a, **k: SimpleNamespace(accept=False))
    ing = Ingestor(None, _state(), prefix="nd", gate=True)
    assert _ingest(ing) is False and ing.dropped == 1 and not pub

    monkeypatch.setattr(ing_mod, "accept_source", lambda *a, **k: SimpleNamespace(accept=True))
    ing2 = Ingestor(None, _state(), prefix="nd", gate=True)
    assert _ingest(ing2) is True and len(pub) == 1


def test_body_none_fetches_but_empty_body_skips_without_fetch(monkeypatch):
    pub = _patch_publish(monkeypatch)
    _identity_clean(monkeypatch)
    fetched: list[str] = []
    monkeypatch.setattr(ing_mod, "fetch_article", lambda url: (fetched.append(url) or ("fetched body", "img.png")))

    ing = Ingestor(None, _state(), prefix="gd", gate=False)
    assert _ingest(ing, url="http://x/1", body=None) is True  # None → fetch the body
    assert pub[0]["data"]["body"] == "fetched body" and pub[0]["data"]["image_url"] == "img.png"

    assert _ingest(ing, url="http://x/2", body="") is False  # "" → skip, never fetch (NewsData path)
    assert fetched == ["http://x/1"] and len(pub) == 1


def test_clean_false_publishes_raw_body(monkeypatch):
    pub = _patch_publish(monkeypatch)
    cleaned: list[int] = []
    monkeypatch.setattr(ing_mod, "clean_article", lambda t, b, s: (cleaned.append(1) or ("X", "X")))
    ing = Ingestor(None, _state(), prefix="nd", gate=False, clean=False)
    _ingest(ing, title="raw title", body="raw body")
    assert not cleaned  # NewsData: API bodies arrive clean, clean_article is skipped
    assert pub[0]["data"]["title"] == "raw title" and pub[0]["data"]["body"] == "raw body"


def test_extra_fields_merged_and_counted(monkeypatch):
    pub = _patch_publish(monkeypatch)
    _identity_clean(monkeypatch)
    ing = Ingestor(None, _state(), prefix="rss", gate=False)
    _ingest(ing, fields={"provider": "rss", "alignment": "independent", "country": "GB"})
    data = pub[0]["data"]
    assert data["provider"] == "rss" and data["alignment"] == "independent" and data["country"] == "GB"
    assert ing.by_country["GB"] == 1


def test_detect_language_overrides_from_body(monkeypatch):
    pub = _patch_publish(monkeypatch)
    _identity_clean(monkeypatch)
    ing = Ingestor(None, _state(), prefix="loc", gate=False)
    _ingest(ing, language="en", body="texto", detect_language=lambda b: "es")
    assert pub[0]["data"]["language"] == "es"  # apify-locale: metadata lang is unreliable


def test_sources_registry_and_unknown_source_errors():
    assert set(SOURCES) == {"gdelt", "rss", "newsdata", "locales"}
    with pytest.raises(SystemExit):
        asyncio.run(acquire("nope", root=Path(".")))  # bad --source fails before any I/O
