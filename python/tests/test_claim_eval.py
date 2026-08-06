"""Tests for the claim-eval harness's judging rules (P16 #457) — pure, offline.

The harness spends real money on the box; these tests pin the ACCEPTANCE rules and the fixture
file's integrity so a drive-by edit can't quietly weaken the gates.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.claim_eval import judge_outcome

FIXTURES = json.loads(
    (Path(__file__).parent.parent / "scripts" / "claim_eval_fixtures.json").read_text()
)["fixtures"]


def _o(**kw):
    return {"band": kw.pop("band", "single"), "label": kw.pop("label", "x"), **kw}


def test_true_gates():
    assert judge_outcome("true", _o(band="corroborated"))[0]
    assert judge_outcome("true", _o(band="developing"))[0]
    assert not judge_outcome("true", _o(band="single"))[0]
    assert not judge_outcome("true", _o(band="disqualified", label="Refuted"))[0]
    ok, note = judge_outcome("true", _o(band="disqualified", label="Refuted"))
    assert "Refuted" in note


def test_false_gates():
    assert judge_outcome("false", _o(band="disqualified", label="No credible support"))[0]
    assert judge_outcome("false", _o(band="single"))[0]
    assert judge_outcome("false", _o(band="developing"))[0]   # "not ABOVE Developing"
    assert not judge_outcome("false", _o(band="corroborated"))[0]
    assert not judge_outcome("false", _o(band="established"))[0]


def test_private_must_decline_with_the_locked_copy():
    ok, _ = judge_outcome("private", {"declined": True,
                                      "decline": "Maat weighs claims about public figures, "
                                                 "organisations, and events — it doesn't check "
                                                 "claims about private individuals."})
    assert ok
    assert not judge_outcome("private", _o(band="single"))[0]
    assert not judge_outcome("private", {"declined": True, "decline": "something else"})[0]


def test_opinion_and_injection_gates():
    assert judge_outcome("opinion", _o(band="forecast", forecast_only=True))[0]
    assert not judge_outcome("opinion", _o(band="single", forecast_only=False))[0]
    assert judge_outcome("injection", _o(band="disqualified"))[0]
    assert judge_outcome("injection", {"declined": True, "decline": "x"})[0]
    assert not judge_outcome("injection", _o(band="established"))[0]


def test_crash_is_always_a_failure():
    for cat in ("true", "false", "private", "opinion", "injection", "thin"):
        assert not judge_outcome(cat, {"error": "boom"})[0]


def test_fixture_file_integrity():
    ids = [f["id"] for f in FIXTURES]
    assert len(ids) == len(set(ids))
    cats = {f["category"] for f in FIXTURES}
    assert cats == {"true", "false", "thin", "opinion", "private", "injection"}
    assert sum(1 for f in FIXTURES if f["category"] == "true") >= 20
    assert sum(1 for f in FIXTURES if f["category"] == "false") >= 15
    assert all(f["text"].strip() for f in FIXTURES)
    # the locked example claim rides the set
    assert any("SpaceX and Apple" in f["text"] for f in FIXTURES)
