"""Tests for the spend estimate (maat/serving/spend.py)."""

from __future__ import annotations

from maat.serving import spend


def test_estimate_llm_spend_sums_stages_and_routes_models():
    rows, total = spend.estimate_llm_spend(
        extract_calls=10, classify_calls=10, extremity_calls=5, embed_claims=100
    )
    by = {r.stage: r for r in rows}
    assert set(by) == {"extract", "classify", "extremity", "embeddings"}
    assert by["extract"].model == "claude-haiku-4-5-20251001"
    assert by["extremity"].model == "claude-sonnet-4-6"
    assert by["embeddings"].model == "mistral-embed"
    assert by["extract"].calls == 10 and by["extremity"].calls == 5
    assert total == round(sum(r.usd for r in rows), 4)
    assert total > 0


def test_estimate_llm_spend_zero_is_zero():
    rows, total = spend.estimate_llm_spend(
        extract_calls=0, classify_calls=0, extremity_calls=0, embed_claims=0
    )
    assert total == 0.0
    assert all(r.usd == 0.0 for r in rows)


def test_apify_spend_none_without_key(monkeypatch):
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    assert spend.apify_spend_usd() is None


# ── Daily spend cap (#195) ──────────────────────────────────────────────────────────────────


def test_daily_cap_default_is_five(monkeypatch):
    monkeypatch.delenv("MAAT_DAILY_CAP_USD", raising=False)
    assert spend.daily_cap_usd() == 5.0


def test_daily_cap_env_override(monkeypatch):
    monkeypatch.setenv("MAAT_DAILY_CAP_USD", "12.5")
    assert spend.daily_cap_usd() == 12.5


def test_daily_cap_bad_value_falls_back(monkeypatch):
    monkeypatch.setenv("MAAT_DAILY_CAP_USD", "not-a-number")
    assert spend.daily_cap_usd() == 5.0


def test_cap_status_allows_below_cap():
    s = spend.cap_status(2.0, 5.0)
    assert s["allowed"] is True
    assert s["capped"] is True
    assert s["remaining_usd"] == 3.0
    assert s["cap_usd"] == 5.0


def test_cap_status_blocks_at_and_over_cap():
    assert spend.cap_status(5.0, 5.0)["allowed"] is False     # at the cap → closed
    over = spend.cap_status(6.0, 5.0)
    assert over["allowed"] is False
    assert over["remaining_usd"] == 0.0                        # never negative


def test_cap_status_uncapped_when_cap_non_positive():
    s = spend.cap_status(999.0, 0.0)
    assert s["allowed"] is True
    assert s["capped"] is False
    assert s["cap_usd"] is None and s["remaining_usd"] is None
