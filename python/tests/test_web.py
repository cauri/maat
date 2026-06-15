"""Console tests (§5.7, P8) — story rollup, confidence derivation, audit render. No DB."""

import datetime as dt

from maat.pipeline.corroborate import confidence_read
from maat.web.app import (
    _audit_page,
    _claim_badges,
    _confidence_label,
    _group_stories,
    _nav,
    derivation_explain,
)


def test_confidence_label_tiers():
    assert _confidence_label(0.97) == ("Well corroborated", "hi")
    assert _confidence_label(0.75)[1] == "mid"
    assert _confidence_label(0.45)[1] == "lo"
    # the floor: a thin claim is flagged, not presented as established
    assert _confidence_label(0.32) == ("Thinly sourced", "floor")


def test_group_stories_splits_by_article_overlap_and_picks_headline():
    # two stories: resignation clusters share article a2; gold clusters share b1; no overlap
    clusters = [
        {"sources": ["S1"], "originators": [["a2"]], "confidence": 0.60},  # resignation secondary
        {"sources": ["S1", "S2", "S3"], "originators": [["a1", "a2", "a3"]], "confidence": 0.97},
        {"sources": ["X1", "X2", "X3"], "originators": [["b1", "b2", "b3"]], "confidence": 0.32},
        {"sources": ["X1"], "originators": [["b1"]], "confidence": 0.60},  # gold secondary
    ]
    stories = _group_stories(clusters)
    assert len(stories) == 2
    # the headline is the most-asserted claim (most sources), even when it's the LOW-confidence
    # one — the gold story leads with the 0.32 extraordinary claim, not its 0.60 footnote
    headlines = {round(s[0]["confidence"] * 100) for s in stories}
    assert headlines == {97, 32}


def test_derivation_explain_tracks_confidence_read():
    # F2: the operator-facing derivation must report the same number confidence_read computes
    s = derivation_explain(2, False, "notable")
    assert "2 independent originators" in s
    assert "prior: notable" in s
    assert f"{round(confidence_read(2, False, 'notable') * 100)}% confidence" in s
    assert "primary source" not in s
    # singular grammar + the primary-source lift called out
    s1 = derivation_explain(1, True, "extraordinary")
    assert "1 independent originator ·" in s1
    assert "primary source" in s1


def test_claim_badges_flag_corrections_and_laundering():
    c = {
        "in_headline": True, "voice": "attributed", "speaker": "Araghchi", "kind": "fact",
        "is_synthesis": False, "horizon": None, "corrected": True,
        "laundering_flag": "endorsement",
    }
    b = _claim_badges(c)
    assert "headline" in b and "said · Araghchi" in b and "fact" in b
    assert "corrected" in b and "laundering · endorsement" in b


def test_audit_page_strips_prefix_shows_reason_and_extras_and_handles_empty():
    assert "No operator actions yet" in _audit_page([])
    rows = [
        {
            "type": "admin.cluster.split",
            "data": {"target": "c1", "actor": "operator", "reason": "over-merged", "into": ["x", "y"]},
            "created_at": dt.datetime(2026, 6, 14, 22, 5),
        }
    ]
    out = _audit_page(rows)
    assert "cluster.split" in out  # admin. prefix stripped for display
    assert "over-merged" in out
    assert "into=" in out  # non-standard fields surfaced


def test_nav_marks_active_tab():
    assert 'class="on"' in _nav("content")
    assert "Audit" in _nav("audit")
    assert "Eval" in _nav("content")  # A4a tab present


def test_eval_page_surfaces_pass_fail_and_metrics():
    # A4a renders the REAL eval harness output (no rebuild). evaluate() is pure over dicts.
    from maat.evals import evaluate
    from maat.web.app import _eval_page

    clusters = [
        {
            "fact": "Minister X resigned", "sources": ["AFP", "Daily"], "originators": [["a", "b"]],
            "independent_originators": 2, "has_primary": False, "confidence": 0.75,
            "extremity": "notable",
        }
    ]
    claims = [{"kind": "fact"}, {"kind": "projection"}]
    ok = evaluate(clusters, claims, {"resign": {"match": "resigned", "independent_originators": 2}})
    out = _eval_page(ok, "", "")
    assert "PASS" in out and "1/1 golden stories" in out
    assert "2 (want 2)" in out  # the golden check detail is surfaced verbatim

    bad = evaluate(clusters, claims, {"resign": {"match": "resigned", "independent_originators": 9}})
    assert "FAIL" in _eval_page(bad, "", "")


def test_eval_page_otlp_note_and_missing_fixtures():
    from maat.web.app import _eval_page

    assert "OTLP tracing off" in _eval_page(None, "no fixtures", "")
    assert "no fixtures" in _eval_page(None, "no fixtures", "")
    assert "open trace UI" in _eval_page(None, "x", "http://localhost:4318")


def test_stage_summary_maps_event_types_to_stages():
    from maat.web.app import stage_summary

    rows = stage_summary(
        {"claims.extracted": {"n": 5, "last": None}, "cluster.corroborated": {"n": 2, "last": None}}
    )
    by = {r["label"]: r for r in rows}
    assert by["Extract"]["count"] == 5
    assert by["Corroborate"]["count"] == 2
    assert by["Classify"]["count"] == 0  # an absent event type reads as zero, not missing
    assert all("make" in r["cmd"] for r in rows)


def test_runs_page_shows_stages_dead_letters_and_recent():
    from maat.web.app import _runs_page, stage_summary

    stages = stage_summary({"article.ingested": {"n": 3, "last": dt.datetime(2026, 6, 15, 9, 0)}})
    proj = {"articles": 3, "claims": 0, "clusters": 0, "events": 3}
    recent = [{"type": "article.ingested", "stream_id": "a1", "created_at": dt.datetime(2026, 6, 15, 9, 0)}]
    dead = [
        {"type": "cluster.corroborated", "stream_id": "c1", "error": "boom",
         "created_at": dt.datetime(2026, 6, 15, 9, 1)}
    ]
    out = _runs_page(stages, proj, recent, dead)
    assert "Acquire / ingest" in out
    assert "Dead-letter" in out and "boom" in out
    assert "Recent events" in out


def test_config_knobs_sourced_from_live_code():
    from maat.config import KNOBS_BY_KEY, groups
    from maat.providers.seam import CLAUDE_JUDGE

    assert {"model.judge", "gate.floor", "cluster.same_fact"} <= set(KNOBS_BY_KEY)
    assert KNOBS_BY_KEY["model.judge"]["core"] is True
    assert KNOBS_BY_KEY["model.bulk"]["core"] is False
    assert KNOBS_BY_KEY["model.judge"]["default"] == CLAUDE_JUDGE  # not invented
    assert "Model routing" in groups()


def test_config_page_shows_default_override_and_signoff_guard():
    from maat.web.app import _config_page

    out = _config_page(
        {"gate.floor": {"value": "0.35", "reason": "too strict", "at": dt.datetime(2026, 6, 15, 10, 0)}}
    )
    assert "Config" in out
    assert "0.35" in out and "pending sign-off" in out  # the proposal is shown, marked pending
    assert "not auto-applied" in out  # the guardrail is surfaced to the operator
    assert "core · sign-off" in out  # core knobs flagged


def test_wire_collapsed_sources_flags_only_multi_article_groups():
    from maat.web.app import wire_collapsed_sources

    id_to_source = {"a1": "AFP", "a2": "Daily News", "a3": "Indie Times"}
    clusters = [{"originators": [["a1", "a2"], ["a3"]]}]  # a1+a2 are one wire node; a3 independent
    assert wire_collapsed_sources(clusters, id_to_source) == {"AFP", "Daily News"}


def test_sources_page_registry_badges_and_proposal_note():
    from maat.web.app import _sources_page

    srcs = [
        {"source": "European Central Bank", "n": 3, "last": dt.datetime(2026, 6, 15), "langs": ["en"]},
        {"source": "AFP", "n": 9, "last": dt.datetime(2026, 6, 15), "langs": ["en", "fr"]},
    ]
    out = _sources_page(srcs, {"AFP"}, {"AFP": {"status": "deny", "reason": "wire"}}, {"AFP": "Wire"})
    assert "European Central Bank" in out and "primary" in out  # primary-source role detected
    assert "wire-collapsed" in out and "denied" in out and "group · Wire" in out
    assert "proposals" in out  # enforcement-deferred guardrail surfaced


def test_nav_includes_all_p8_tabs():
    from maat.web.app import _nav

    n = _nav("content")
    for label in ("Content", "Runs", "Config", "Sources", "Eval", "Audit"):
        assert label in n
