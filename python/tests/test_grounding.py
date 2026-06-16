"""Primary-source grounding (#228) — judge, confidence refinement, refutation, trajectory.

Pure / mocked — no DB, no live LLM (the judge's model call is monkeypatched), per the convention
that the core is tested without IO.
"""

from __future__ import annotations

from datetime import datetime, timezone

import maat.pipeline.grounding as g
from maat.learning.calibration import observations_from_history, resolve_outcome
from maat.learning.harvest import harvest
from maat.learning.trajectory import _snapshot_to_dict
from maat.pipeline.corroborate import confidence_read

_AT = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


class _Reply:
    def __init__(self, text):
        self.text = text


# --- the judge ---------------------------------------------------------------------------


def test_judge_maps_verdicts(monkeypatch):
    monkeypatch.setattr(g, "claude_complete",
                        lambda *a, **k: _Reply('{"verdict":"SUPPORTED","evidence":"raised by 50bp"}'))
    verdict, evidence = g.judge_grounding("ECB raised rates 50bp", "ecb.europa.eu", "…raised by 50 basis points…")
    assert verdict == "supported"
    assert evidence == "raised by 50bp"


def test_judge_contradicted(monkeypatch):
    monkeypatch.setattr(g, "claude_complete",
                        lambda *a, **k: _Reply('prose {"verdict":"CONTRADICTED","evidence":"25bp"} more'))
    assert g.judge_grounding("ECB raised 50bp", "ecb", "raised by 25 basis points")[0] == "contradicted"


def test_judge_empty_input_returns_blank():
    assert g.judge_grounding("", "ecb", "body") == ("", "")
    assert g.judge_grounding("fact", "ecb", "") == ("", "")


def test_judge_bad_json_or_unknown_verdict_returns_blank(monkeypatch):
    monkeypatch.setattr(g, "claude_complete", lambda *a, **k: _Reply("not json at all"))
    assert g.judge_grounding("fact", "ecb", "body") == ("", "")
    monkeypatch.setattr(g, "claude_complete", lambda *a, **k: _Reply('{"verdict":"MAYBE"}'))
    assert g.judge_grounding("fact", "ecb", "body")[0] == ""


# --- confidence refinement (decision 1) --------------------------------------------------


def test_supported_keeps_the_primary_lift():
    assert confidence_read(2, True, "notable", grounding="supported") == confidence_read(2, True, "notable")


def test_not_addressed_withholds_the_lift():
    withheld = confidence_read(2, True, "notable", grounding="not_addressed")
    assert withheld < confidence_read(2, True, "notable", grounding="supported")
    assert withheld == confidence_read(2, False, "notable")  # as if the primary weren't there


def test_contradicted_penalises_below_not_addressed():
    assert (confidence_read(2, True, "notable", grounding="contradicted")
            < confidence_read(2, True, "notable", grounding="not_addressed"))


def test_grounding_none_is_unchanged_behaviour():
    assert confidence_read(2, True, "notable", grounding=None) == confidence_read(2, True, "notable")


# --- refutation (decision 2) -------------------------------------------------------------


def test_contradicted_resolves_refuted():
    assert resolve_outcome(1, 3, latest_has_primary=True, corrected=False, grounding="contradicted") == "refuted"


def test_supported_still_confirms_not_refutes():
    assert resolve_outcome(1, 3, latest_has_primary=True, corrected=False, grounding="supported") == "confirmed"


# --- on the trajectory (decision 3) ------------------------------------------------------


def test_harvest_payload_carries_grounding():
    row = {"id": "c1", "fact": "F", "independent_originators": 2, "has_primary": True, "grounding": "contradicted"}
    assert harvest([row], at=_AT)[0]["data"]["grounding"] == "contradicted"


def test_harvest_payload_grounding_defaults_none():
    row = {"id": "c1", "fact": "F", "independent_originators": 1, "has_primary": False}
    assert harvest([row], at=_AT)[0]["data"]["grounding"] is None


def test_snapshot_row_carries_grounding():
    r = {"fact": "F", "independent_originators": 2, "has_primary": True, "extremity": "notable",
         "confidence": 0.5, "sources": ["a"], "originators": [["x"]], "corrected": False,
         "grounding": "supported", "cluster_id": "c1", "harvested_at": _AT}
    assert _snapshot_to_dict(r)["grounding"] == "supported"


def test_contradicted_grounding_flows_to_refuted_through_the_fold():
    # A snapshot trajectory whose latest point a primary CONTRADICTS resolves the fact REFUTED —
    # the automated refutation signal #228 adds, read straight off cluster_snapshots.
    hist = [
        {"fact": "F", "independent_originators": 1, "has_primary": True, "extremity": "notable", "grounding": None},
        {"fact": "F", "independent_originators": 3, "has_primary": True, "extremity": "notable", "grounding": "contradicted"},
    ]
    assert observations_from_history(hist)[0].outcome == "refuted"
