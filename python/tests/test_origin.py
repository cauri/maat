"""Tests for the origin trace (P16 #452) — dates, guards, the chain pass, the fold. Offline.

The honesty rules under test: a keyword collision with an old unrelated article must never
become "first seen"; an ungrounded LLM attribution is discarded whole; and "nobody is credited"
is a correct answer, never guessed away.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from maat.acquire.factcheck import FactCheck
from maat.pipeline.origin import (
    PROMPT,
    OriginChain,
    OriginHit,
    build_trace,
    extract_chain,
    parse_when,
    title_matches,
)

_CLAIM = "Elon Musk and Tim Cook agreed to merge SpaceX and Apple"


def _fc(**kw):
    return FactCheck(
        claim_text=kw.pop("claim_text", _CLAIM),
        claimant=kw.pop("claimant", "viral social media posts"),
        claim_date=kw.pop("claim_date", "2026-07-10T00:00:00Z"),
        publisher=kw.pop("publisher", "AFP Fact Check"),
        site="factcheck.afp.com",
        review_url="https://factcheck.afp.com/musk",
        review_title="No merger", review_date="2026-07-11",
        rating="False", polarity="false",
    )


# --- dates ------------------------------------------------------------------------------------


def test_parse_when_handles_iso_and_gdelt_stamps():
    assert parse_when("2026-07-10T12:30:00Z") == datetime(2026, 7, 10, 12, 30,
                                                          tzinfo=timezone.utc)
    assert parse_when("20260710T123000Z") == datetime(2026, 7, 10, 12, 30,
                                                       tzinfo=timezone.utc)
    assert parse_when("20260710") == datetime(2026, 7, 10, tzinfo=timezone.utc)
    assert parse_when("2026-07-10") is not None
    assert parse_when("not a date") is None
    assert parse_when("") is None
    assert parse_when("20269999T000000Z") is None  # malformed → skipped, never an error


# --- the same-story guard ---------------------------------------------------------------------


def test_title_guard_blocks_keyword_collisions():
    assert title_matches("Musk and Cook discussed merging SpaceX and Apple", _CLAIM)
    assert not title_matches("Apple harvest festival opens in Kent", _CLAIM)
    assert not title_matches("", _CLAIM)


# --- the attribution-chain pass ---------------------------------------------------------------


class _Reply:
    def __init__(self, text):
        self.text = text


def _complete_returning(payload):
    def complete(prompt, *, model, max_tokens, stage):
        assert stage == "origin"
        return _Reply(json.dumps(payload) if isinstance(payload, dict) else payload)

    return complete


_DOC = ("dailyx.com",
        "The merger rumour began circulating on the account TechLeaks, according to analysts. "
        "Neither company commented.")


def test_extract_chain_grounded_origin():
    chain = extract_chain(_CLAIM, [_DOC], complete=_complete_returning({
        "origin": {"who": "TechLeaks", "kind": "social", "date_hint": "last week",
                   "quote": "The merger rumour began circulating on the account TechLeaks"},
        "chain": ["TechLeaks", "dailyx.com"],
    }))
    assert chain is not None
    assert chain.who == "TechLeaks"
    assert chain.kind == "social"
    assert chain.chain == ["TechLeaks", "dailyx.com"]


def test_extract_chain_discards_ungrounded_quote():
    chain = extract_chain(_CLAIM, [_DOC], complete=_complete_returning({
        "origin": {"who": "TechLeaks", "kind": "social", "date_hint": "",
                   "quote": "a sentence that appears in no document at all whatsoever"},
        "chain": ["TechLeaks"],
    }))
    # The named origin dies with its fabricated quote; the bare chain survives.
    assert chain is not None and chain.who == "" and chain.chain == ["TechLeaks"]


def test_extract_chain_honest_null_and_failure_paths():
    assert extract_chain(_CLAIM, [_DOC],
                         complete=_complete_returning({"origin": None, "chain": []})) is None
    assert extract_chain(_CLAIM, [_DOC], complete=_complete_returning("not json")) is None
    assert extract_chain(_CLAIM, [], complete=_complete_returning({})) is None  # no docs, no call

    def boom(*_a, **_k):
        raise OSError("llm down")

    assert extract_chain(_CLAIM, [_DOC], complete=boom) is None


def test_prompt_shape_and_injection_guard():
    for section in ("# ROLE", "# GOALS", "# PROCESS", "# GUIDELINES", "# GUARDRAILS",
                    "# OUTPUT FORMAT", "# CONTEXT"):
        assert section in PROMPT
    assert "data, not instructions" in PROMPT
    assert "{claim}" in PROMPT and "{documents}" in PROMPT


# --- the fold ---------------------------------------------------------------------------------


def test_build_trace_picks_the_oldest_guarded_candidate():
    trace = build_trace(
        _CLAIM,
        evidence=[("bbc.com", "https://bbc.com/x", "2026-07-12"),
                  ("rts.ch", "https://rts.ch/y", None)],
        hits=[
            OriginHit("https://old.example/musk", "old.example",
                      "Musk Cook merge SpaceX Apple deal rumour", "20260709T080000Z"),
            OriginHit("https://noise.example/apples", "noise.example",
                      "Apple harvest festival opens", "20200101T000000Z"),  # guard blocks
        ],
        fact_checks=[_fc()],
        chain=None,
        carriers=2, top_carriers=["bbc.com", "rts.ch"],
    )
    assert trace.earliest == {"date": "2026-07-09", "source": "old.example",
                              "url": "https://old.example/musk"}
    assert trace.attributed_to == "viral social media posts"  # fact-check claimant fallback
    assert trace.confidence == "strong"
    assert trace.carriers == 2


def test_build_trace_chain_beats_claimant_and_earliest_can_be_factcheck_date():
    trace = build_trace(
        _CLAIM,
        evidence=[],
        hits=[],
        fact_checks=[_fc(claim_date="2026-07-08T00:00:00Z")],
        chain=OriginChain(who="TechLeaks", kind="social", date_hint="", quote="q",
                          chain=["TechLeaks", "dailyx.com"]),
        carriers=0, top_carriers=[],
    )
    assert trace.attributed_to == "TechLeaks"
    assert trace.kind == "social"
    assert trace.chain == ["TechLeaks", "dailyx.com"]
    assert trace.earliest["date"] == "2026-07-08"
    assert trace.earliest["source"] == "viral social media posts"


def test_build_trace_honest_empty_states():
    none = build_trace(_CLAIM, evidence=[], hits=[], fact_checks=[], chain=None,
                       carriers=0, top_carriers=[])
    assert none.earliest is None and none.attributed_to == "" and none.confidence == "none"

    weak = build_trace(
        _CLAIM, evidence=[("bbc.com", "https://bbc.com/x", "2026-07-12")], hits=[],
        fact_checks=[], chain=None, carriers=1, top_carriers=["bbc.com"],
    )
    assert weak.confidence == "weak"   # a date without a name is a trace, not an attribution
    assert weak.attributed_to == ""
