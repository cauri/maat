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


def test_grounding_agent_bounds_llm_work_per_tick():
    """#419 — incremental is not the same as bounded.

    `done` (the CLUSTER_GROUNDED events) stops the agent re-judging a cluster it already judged. It
    says nothing about how many it judges in ONE tick. Each is an LLM call — measured at 3.1s on the
    box — against a 1200s step timeout, so ~393 fit. A restarted engine faces a far bigger cold
    backlog (the corroborate run alone yields 10,577 clusters), and unbudgeted the step is killed
    mid-work and reported TIMEOUT every tick until it drains: a false alarm that looks exactly like
    the 27-day outage.
    """
    import maat.agents.grounding_agent as agent

    assert agent._MAX_CLUSTERS > 0
    # sized against the MEASURED 3.1s/cluster, inside the 1200s step with headroom for the load
    assert agent._MAX_CLUSTERS * 3.1 < 1200, "the per-tick budget cannot fit the step's 1200s timeout"


def test_grounding_agent_reports_the_backlog_it_leaves():
    """A bounded run that prints only what it DID reads as a complete one (#419).

    That is the exact shape of the failure this issue is about — the pipeline inferring success from
    the absence of a complaint. If the agent defers work, the operator has to be able to see it.
    """
    import inspect

    import maat.agents.grounding_agent as agent

    src = inspect.getsource(agent.main)
    assert "left" in src and "next tick" in src, "the report must state the deferred backlog"
