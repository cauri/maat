"""NewsData.io client tests — pure parsing + gated search/pagination (no network)."""

from __future__ import annotations

from maat.acquire import newsdata


def test_available_reflects_key(monkeypatch):
    monkeypatch.delenv("MAAT_NEWSDATA_KEY", raising=False)
    assert newsdata.available() is False
    monkeypatch.setenv("MAAT_NEWSDATA_KEY", "k")
    assert newsdata.available() is True


def test_parse_results_filters_and_prefers_content():
    rows = [
        {"link": "https://lenta.ru/x", "title": "T1", "content": "F" * 300,
         "description": "short", "language": "ru", "country": ["russia"], "source_id": "lenta"},
        {"link": "https://x.com/y", "title": "no body", "content": "", "description": "tiny"},  # too short
        {"title": "no url", "content": "B" * 300},                                              # no url
        {"link": "https://elpais.com/z", "title": "T3", "description": "D" * 250,
         "language": "es", "country": "spain"},                                                 # desc fallback, scalar country
    ]
    out = newsdata.parse_results(rows)
    assert [a.url for a in out] == ["https://lenta.ru/x", "https://elpais.com/z"]
    a0 = out[0]
    assert a0.body.startswith("F") and a0.domain == "lenta.ru"   # content preferred over description
    assert a0.language == "ru" and a0.country == "russia"        # list normalised to first
    assert out[1].body.startswith("D") and out[1].country == "spain"  # description fallback, scalar country


def test_search_returns_empty_without_key(monkeypatch):
    monkeypatch.delenv("MAAT_NEWSDATA_KEY", raising=False)
    assert newsdata.search("anything") == []


def test_search_paginates_and_caps(monkeypatch):
    monkeypatch.setenv("MAAT_NEWSDATA_KEY", "k")
    pages = [
        {"results": [{"link": f"https://s/{i}", "title": "t", "content": "C" * 300} for i in range(3)],
         "nextPage": "p2"},
        {"results": [{"link": f"https://s/{i}", "title": "t", "content": "C" * 300} for i in range(3, 6)],
         "nextPage": None},
    ]
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def fake_get(url, *, params=None, headers=None, timeout=None):
        # key travels in the X-ACCESS-KEY header, never the URL/params (no leak in logs/errors)
        assert "apikey" not in params and headers.get("X-ACCESS-KEY") == "k"
        if calls["n"] == 1:
            assert params.get("page") == "p2"  # nextPage threaded through
        resp = _Resp(pages[calls["n"]])
        calls["n"] += 1
        return resp

    monkeypatch.setattr(newsdata.httpx, "get", fake_get)
    out = newsdata.search("q", language="ru", max_results=5, pages=3)
    assert len(out) == 5            # capped at max_results across 2 fetched pages
    assert calls["n"] == 2          # stopped when nextPage was None


def test_search_uses_domainurl_and_header_auth(monkeypatch):
    monkeypatch.setenv("MAAT_NEWSDATA_KEY", "secret")
    seen = {}

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"results": [], "nextPage": None}

    def fake_get(url, *, params=None, headers=None, timeout=None):
        seen["params"] = params
        seen["headers"] = headers
        return _R()

    monkeypatch.setattr(newsdata.httpx, "get", fake_get)
    newsdata.search("", domain="bbc.com", max_results=10)
    assert seen["params"].get("domainurl") == "bbc.com"   # domain filter → domainurl
    assert "domain" not in seen["params"]
    assert "apikey" not in seen["params"]                 # key NOT in the URL
    assert seen["headers"]["X-ACCESS-KEY"] == "secret"    # key in the header instead


def test_search_min_chars_zero_keeps_metadata_only_rows(monkeypatch):
    monkeypatch.setenv("MAAT_NEWSDATA_KEY", "k")
    payload = {"results": [{"link": "https://bbc.com/x", "title": "t",
                            "content": "stub", "description": "short"}], "nextPage": None}

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr(newsdata.httpx, "get", lambda *a, **k: _R())
    # default min_chars=200 drops the metadata-only row; min_chars=0 keeps it so the link survives
    assert newsdata.search("", domain="bbc.com") == []
    kept = newsdata.search("", domain="bbc.com", min_chars=0)
    assert len(kept) == 1 and kept[0].url == "https://bbc.com/x"
