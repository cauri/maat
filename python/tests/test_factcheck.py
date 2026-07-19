"""Tests for the Fact Check Tools connector (P16 #451) — parsing + rating polarity, offline."""

from __future__ import annotations

import pytest

from maat.acquire import factcheck
from maat.acquire.factcheck import FactCheck, parse_claims, rating_polarity, search_fact_checks

_RESPONSE = {
    "claims": [
        {
            "text": "Elon Musk and Tim Cook agreed to merge SpaceX and Apple",
            "claimant": "viral social media posts",
            "claimDate": "2026-07-10T00:00:00Z",
            "claimReview": [
                {
                    "publisher": {"name": "AFP Fact Check", "site": "factcheck.afp.com"},
                    "url": "https://factcheck.afp.com/spacex-apple-merger",
                    "title": "No, SpaceX and Apple are not merging",
                    "reviewDate": "2026-07-11T00:00:00Z",
                    "textualRating": "False",
                },
                {
                    "publisher": {"name": "Snopes"},
                    "url": "https://www.snopes.com/fact-check/spacex-apple",
                    "title": "Did Musk and Cook agree to merge?",
                    "textualRating": "Pants on Fire!",
                },
            ],
        },
        {"text": "", "claimReview": [{"url": "https://x.example/skipped"}]},  # no claim text
        {"text": "Claim without any review", "claimReview": []},
        {"text": "Review without a URL", "claimReview": [{"textualRating": "True"}]},
    ]
}


def test_parse_claims_flattens_and_skips_incomplete_entries():
    got = parse_claims(_RESPONSE)
    assert len(got) == 2
    afp, snopes = got
    assert afp.publisher == "AFP Fact Check"
    assert afp.site == "factcheck.afp.com"
    assert afp.claimant == "viral social media posts"     # origin seed for #452
    assert afp.claim_date == "2026-07-10T00:00:00Z"
    assert afp.polarity == "false"
    assert snopes.polarity == "false"                      # "Pants on Fire!" is the false family
    assert snopes.site == "www.snopes.com"                 # derived from the review URL


@pytest.mark.parametrize("rating,polarity", [
    ("False", "false"),
    ("FAKE news", "false"),
    ("No evidence", "false"),
    ("True", "true"),
    ("Accurate", "true"),
    ("Mostly true", "mixed"),     # mixed beats the embedded "true"
    ("Mostly false", "mixed"),
    ("Half true", "mixed"),
    ("Missing context", "mixed"),
    ("Unproven", "mixed"),
    ("Four Pinocchios", "unclear"),
    ("", "unclear"),
])
def test_rating_polarity_closed_set(rating, polarity):
    assert rating_polarity(rating) == polarity


def test_search_fact_checks_returns_empty_on_api_failure(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(factcheck.httpx, "get", boom)
    assert search_fact_checks("anything", key="k") == []


def test_search_fact_checks_parses_response(monkeypatch):
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return _RESPONSE

    def fake_get(url, *, params, headers, timeout):
        seen["url"], seen["params"] = url, params
        return _Resp()

    monkeypatch.setattr(factcheck.httpx, "get", fake_get)
    got = search_fact_checks("SpaceX Apple merger", key="secret", language="en")
    assert len(got) == 2 and all(isinstance(f, FactCheck) for f in got)
    assert seen["params"]["key"] == "secret"
    assert seen["params"]["languageCode"] == "en"


def test_unknown_language_is_not_sent(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"claims": []}

    captured = {}

    def fake_get(url, *, params, headers, timeout):
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(factcheck.httpx, "get", fake_get)
    search_fact_checks("q", key="k", language="unknown")
    assert "languageCode" not in captured["params"]
