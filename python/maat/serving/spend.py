"""Spend accounting for the operator console (#spend).

cauri wants to *see* spend. The exact per-call truth lives in cat-cafe (every LLM call emits an
OTEL span with model + token usage); this module gives the rolled-up **estimate** the console
shows at a glance, plus the **actual** Apify figure from Apify's billing API.

LLM spend is ESTIMATED (cat-cafe has no cost-summary endpoint): we count the calls each stage
made — from the event log, so it's cumulative across ticks — and multiply by a per-call token
estimate and per-model price. Apify spend is ACTUAL (its usage API returns a $ figure). All of
this is display-only; it never feeds veracity.

Prices are $/1M tokens (input, output); update as vendor pricing changes. The pipeline routes
extract+classify to Haiku and extremity to Sonnet (per maat/pipeline/*), embeddings to Mistral.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

# $ per 1M tokens (input, output). Estimates — the source of truth is each vendor's invoice.
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.00, 15.00),          # extract + classify + extremity (all pin Sonnet)
    "claude-haiku-4-5-20251001": (1.00, 5.00),   # retained for reference; no stage runs on it
    "mistral-embed": (0.10, 0.0),                # clustering embeddings
}

# Rough tokens per call, per stage (input, output). Coarse — cat-cafe is exact. Tuned to the
# prompts' shape: extract/classify read the article body (~big input); extremity rates one short
# fact; embeddings are short claim texts. Models track the pipeline's actual pins
# (EXTRACT_MODEL / CLASSIFY_MODEL / EXTREMITY_MODEL = claude-sonnet-4-6) — NOT Haiku, which the
# pipeline does not use (this estimate previously under-counted extract/classify at Haiku prices).
_PER_CALL = {
    "extract": (1500, 600, "claude-sonnet-4-6"),
    "classify": (1800, 400, "claude-sonnet-4-6"),
    "extremity": (300, 150, "claude-sonnet-4-6"),
}
_EMBED_TOKENS_PER_CLAIM = 50  # mistral-embed input per claim text (rough)


@dataclass(frozen=True)
class StageSpend:
    stage: str
    model: str
    calls: int
    usd: float


# ── Daily spend cap (#195) ────────────────────────────────────────────────────────────────────
# cauri signed off on a default $5/day ceiling for operator-triggered pipeline runs: the console
# refuses to KICK OFF a run once today's estimated LLM spend has reached the cap, so a stuck loop
# or an over-eager operator can't burn the budget. Tunable via MAAT_DAILY_CAP_USD; 0 (or negative)
# disables the gate entirely (uncapped). The estimate is the same Haiku/Sonnet stage model used on
# /spend, scoped to today — cat-cafe holds the exact figure.
DEFAULT_DAILY_CAP_USD = 5.0


def daily_cap_usd() -> float:
    """The daily spend ceiling in USD. ``MAAT_DAILY_CAP_USD`` overrides the $5 default; an
    unparseable value falls back to the default. ``<= 0`` means uncapped (the gate is disabled)."""
    raw = os.environ.get("MAAT_DAILY_CAP_USD")
    if raw is None or raw.strip() == "":
        return DEFAULT_DAILY_CAP_USD
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_DAILY_CAP_USD


def cap_status(today_usd: float, cap_usd: float) -> dict:
    """Pure: may a run start under the daily cap? ``cap_usd <= 0`` disables the gate (always allowed).

    Returns a JSON-able dict: ``allowed`` (bool), ``today_usd``, ``cap_usd`` (None if uncapped),
    ``remaining_usd`` (None if uncapped), ``capped`` (bool). A run is allowed while today's spend is
    strictly below the cap — at or over it, the gate closes.
    """
    capped = cap_usd > 0
    allowed = (not capped) or today_usd < cap_usd
    return {
        "allowed": allowed,
        "today_usd": round(today_usd, 4),
        "cap_usd": round(cap_usd, 4) if capped else None,
        "remaining_usd": round(max(0.0, cap_usd - today_usd), 4) if capped else None,
        "capped": capped,
    }


def _llm_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, (0.0, 0.0))
    return (input_tokens / 1_000_000) * pin + (output_tokens / 1_000_000) * pout


def estimate_llm_spend(
    *, extract_calls: int, classify_calls: int, extremity_calls: int, embed_claims: int
) -> tuple[list[StageSpend], float]:
    """Estimate cumulative LLM spend from per-stage call counts (taken from the event log).

    Returns ``(per_stage, total_usd)``. Call counts come from the events log so they're cumulative
    across every clock tick (corroborate re-rates clusters each tick, so extremity_calls grows).
    """
    rows: list[StageSpend] = []
    for stage, calls in (
        ("extract", extract_calls),
        ("classify", classify_calls),
        ("extremity", extremity_calls),
    ):
        tin, tout, model = _PER_CALL[stage]
        usd = calls * _llm_cost(model, tin, tout)
        rows.append(StageSpend(stage=stage, model=model, calls=calls, usd=round(usd, 4)))

    embed_usd = _llm_cost("mistral-embed", embed_claims * _EMBED_TOKENS_PER_CLAIM, 0)
    rows.append(
        StageSpend(stage="embeddings", model="mistral-embed", calls=embed_claims, usd=round(embed_usd, 4))
    )
    total = round(sum(r.usd for r in rows), 4)
    return rows, total


def apify_spend_usd(*, timeout: float = 10.0) -> float | None:
    """Actual Apify spend this billing cycle (USD), via the Apify usage API. None if unavailable."""
    token = os.environ.get("APIFY_API_KEY")
    if not token:
        return None
    try:
        r = httpx.get(
            "https://api.apify.com/v2/users/me/usage/monthly",
            params={"token": token},
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        usd = (
            data.get("totalUsageCreditsUsdAfterVolumeDiscount")
            or data.get("totalUsageCreditsUsdBeforeVolumeDiscount")
        )
        return round(float(usd), 4) if usd is not None else None
    except Exception:  # noqa: BLE001 — billing API is best-effort; never break the console
        return None
