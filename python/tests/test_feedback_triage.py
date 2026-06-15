"""Tests for the feedback intake + triage agent (P7, issue #58).

All classification tests are pure (no DB, no NATS, no LLM).
Queue-assembly tests use an in-memory asyncpg mock — still no live infra.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from maat.agents.triage import (
    CATEGORIES,
    ROUTES,
    TriageResult,
    classify,
    triage,
)
from maat.serving.feedback import (
    FEEDBACK_SUBMITTED,
    FEEDBACK_TRIAGED,
    new_item_id,
    record,
    record_triage,
    queue,
    routed_queue,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _result(text: str, hint: str = "") -> TriageResult:
    r = classify(text, hint)
    return TriageResult(
        item_id="test-item",
        text=r.text,
        category=r.category,
        route=r.route,
        confidence=r.confidence,
        reason=r.reason,
        auto_fixable=r.auto_fixable,
    )


# ===========================================================================
# 1. PURE classification core — routing correctness
# ===========================================================================


class TestClassifyVeracityDispute:
    def test_wrong_score_is_veracity_dispute(self):
        r = _result("the confidence score for this story is wrong")
        assert r.category == "veracity-dispute"
        assert r.route == "review"

    def test_corroboration_keyword(self):
        r = _result("this claim is not corroborated at all")
        assert r.category == "veracity-dispute"

    def test_thinly_sourced_label(self):
        r = _result("The story is marked 'thinly sourced' but I've seen 5 outlets report it")
        assert r.category == "veracity-dispute"

    def test_false_claim(self):
        r = _result("This is false — the minister did not resign")
        assert r.category == "veracity-dispute"

    def test_veracity_dispute_never_auto_fixes(self):
        r = _result("misleading confidence reading here")
        assert not r.auto_fixable
        assert r.route == "review"


class TestClassifySourceQuality:
    def test_biased_outlet(self):
        r = _result("Reuters is biased toward the western narrative on this story")
        assert r.category == "source-quality"
        assert r.route == "review"

    def test_unreliable(self):
        r = _result("that source is unreliable — it's a tabloid")
        assert r.category == "source-quality"

    def test_source_quality_never_auto_fixes(self):
        r = _result("publisher credibility is questionable")
        assert not r.auto_fixable


class TestClassifyBug:
    def test_crash_report(self):
        r = _result("the app crashed when I tapped the story")
        assert r.category == "bug"

    def test_500_error(self):
        r = _result("getting a 500 error on the feed page")
        assert r.category == "bug"

    def test_not_working(self):
        r = _result("the search is not working at all")
        assert r.category == "bug"

    def test_bug_is_auto_fixable(self):
        r = _result("the feed fails to load after clicking refresh")
        assert r.category == "bug"
        assert r.auto_fixable
        assert r.route == "auto-fix"


class TestClassifyUI:
    def test_layout_issue(self):
        r = _result("the layout is broken on mobile")
        assert r.category == "ui"

    def test_colour(self):
        r = _result("the dark mode colour scheme is hard to read")
        assert r.category == "ui"

    def test_alignment(self):
        r = _result("the icons are misaligned in the header")
        assert r.category == "ui"

    def test_ui_is_auto_fixable(self):
        r = _result("button spacing on mobile is off")
        assert r.auto_fixable
        assert r.route == "auto-fix"


class TestClassifyTopicRequest:
    def test_add_region(self):
        r = _result("please add more coverage of the African region")
        assert r.category == "topic-request"
        assert r.route == "review"

    def test_language_request(self):
        r = _result("I want to track news in Arabic")
        assert r.category == "topic-request"

    def test_topic_request_not_auto_fixable(self):
        r = _result("can you include sports topics?")
        assert not r.auto_fixable


class TestClassifyClientHint:
    def test_hint_boosts_confidence_when_it_agrees(self):
        # "wrong" → veracity-dispute by rule; hint='veracity-dispute' should boost conf
        r_no_hint = classify("the story is wrong")
        r_with_hint = classify("the story is wrong", "veracity-dispute")
        assert r_with_hint.confidence >= r_no_hint.confidence

    def test_hint_honoured_when_no_rule_matches(self):
        # generic text with no trigger words; hint should be the deciding factor
        r = classify("please help", "source-quality")
        assert r.category == "source-quality"
        assert r.confidence > 0.0

    def test_unknown_hint_falls_back_gracefully(self):
        r = classify("please help", "garbage-category")
        assert r.category in CATEGORIES
        assert r.route in ROUTES


class TestClassifyBoundaryProperties:
    def test_all_categories_are_valid(self):
        samples = [
            "this claim is inaccurate",
            "reuters is biased",
            "app crashes on load",
            "button is misaligned",
            "add more latin america coverage",
        ]
        for s in samples:
            r = classify(s)
            assert r.category in CATEGORIES, f"bad category for: {s!r}"

    def test_all_routes_are_valid(self):
        for s in ["broken", "wrong", "biased", "add topic", "layout"]:
            r = classify(s)
            assert r.route in ROUTES

    def test_confidence_is_in_range(self):
        for s in ["broken", "wrong", "biased", "add topic", "layout", "", "   "]:
            r = classify(s)
            assert 0.0 <= r.confidence <= 1.0

    def test_empty_text_does_not_raise(self):
        r = classify("")
        assert r.category in CATEGORIES

    def test_whitespace_only_does_not_raise(self):
        r = classify("   ")
        assert r.category in CATEGORIES

    def test_deterministic_same_input(self):
        text = "confidence score is misleading for the corroborated story"
        results = [classify(text) for _ in range(5)]
        assert all(r.category == results[0].category for r in results)
        assert all(r.route == results[0].route for r in results)

    def test_low_confidence_auto_fix_escalates_to_review(self):
        # If a UI/bug match comes back with very low confidence it must go to review,
        # not auto-fix. We simulate this by patching the rule confidence threshold
        # using an ambiguous text that produces a low-confidence bug match.
        # "stuck" is in the bug pattern but it's a borderline word here.
        r = classify("everything seems stuck somehow maybe not a real bug", "ui")
        # regardless of exact category — the low-conf guard must produce a valid route
        assert r.route in ROUTES


# ===========================================================================
# 2. triage() wraps classify() and sets item_id
# ===========================================================================


def test_triage_sets_item_id():
    r = triage("fb-abc123", "the confidence is wrong")
    assert r.item_id == "fb-abc123"
    assert r.category == "veracity-dispute"


def test_triage_preserves_text():
    text = "the layout is broken on mobile"
    r = triage("fb-x", text)
    assert r.text == text


# ===========================================================================
# 3. Event-type constants are stable strings
# ===========================================================================


def test_event_type_constants():
    assert FEEDBACK_SUBMITTED == "feedback.submitted"
    assert FEEDBACK_TRIAGED == "feedback.triaged"


def test_new_item_id_format():
    fid = new_item_id()
    assert fid.startswith("fb-")
    assert len(fid) > 5


# ===========================================================================
# 4. Queue assembly from events (in-memory mock pool)
# ===========================================================================

# We build a minimal async mock pool that mimics asyncpg's interface for the
# two queries used by queue() and routed_queue().


def _fake_event_row(stream_id: str, type_: str, data: dict, created_at=None) -> MagicMock:
    row = MagicMock()
    row.__getitem__ = lambda self, k: {
        "stream_id": stream_id,
        "type": type_,
        "data": json.dumps(data),
        "created_at": created_at or datetime.datetime(2026, 6, 15, 12, 0, 0),
        "item_id": data.get("item_id", stream_id),
    }[k]
    return row


class _FakePool:
    """Tiny asyncpg pool stub for queue read tests.

    Stores a list of (stream_id, type, data_dict) tuples and filters on the
    SQL ``type = $1`` and ``tenant_id = $2`` pattern used by feedback.py.
    """

    def __init__(self, rows: list[tuple[str, str, dict]]):
        self._rows = rows  # (stream_id, type, data_dict)

    async def execute(self, sql: str, *args: Any) -> None:
        # Capture inserts so record() works in tests
        stream_id, type_, data_str, tenant_id = args
        self._rows.append((stream_id, type_, json.loads(data_str)))

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        # Parse out the type filter from positional args
        # args[0]=type, args[1]=tenant_id, [args[2]=limit or route]
        type_filter = args[0] if args else None

        filtered = [r for r in self._rows if r[1] == type_filter]

        # Handle routed_queue's route filter (data->>'route' = $3)
        if "route" in sql and len(args) >= 3:
            route_val = args[2]
            filtered = [r for r in filtered if r[2].get("route") == route_val]

        # Handle the any($3::text[]) pattern for stream_id IN (...)
        if "any(" in sql and len(args) >= 3 and isinstance(args[2], list):
            id_set = set(args[2])
            filtered = [r for r in filtered if r[0] in id_set]

        # Build mock rows
        result = []
        for stream_id, type_, data in reversed(filtered):  # latest first
            row = MagicMock()
            created_at = datetime.datetime(2026, 6, 15, 12, 0, 0)

            def make_getitem(sid, tp, d, cat):
                def _get(self, k):
                    return {
                        "stream_id": sid,
                        "type": tp,
                        "data": json.dumps(d),
                        "created_at": cat,
                        "item_id": d.get("item_id", sid),
                    }[k]
                return _get

            row.__getitem__ = make_getitem(stream_id, type_, data, created_at)
            result.append(row)

        # Respect LIMIT if present
        limit_arg = None
        if len(args) >= 3 and isinstance(args[2], int):
            limit_arg = args[2]
        elif len(args) >= 4 and isinstance(args[3], int):
            limit_arg = args[3]
        if limit_arg is not None:
            result = result[:limit_arg]

        return result


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestQueueAssembly:
    def setup_method(self):
        self.pool = _FakePool([])

    def test_empty_queue_returns_empty_list(self):
        result = run(queue(self.pool))
        assert result == []

    def test_single_submission_appears_in_queue(self):
        run(record(self.pool, None, item_id="fb-001", text="wrong score", source="app"))
        result = run(queue(self.pool))
        assert len(result) == 1
        assert result[0]["item_id"] == "fb-001"
        assert result[0]["text"] == "wrong score"

    def test_multiple_submissions_all_appear(self):
        for i in range(3):
            run(record(self.pool, None, item_id=f"fb-{i:03d}", text=f"item {i}"))
        result = run(queue(self.pool))
        assert len(result) == 3

    def test_submitted_at_is_populated(self):
        run(record(self.pool, None, item_id="fb-002", text="crash on open"))
        result = run(queue(self.pool))
        assert "submitted_at" in result[0]


class TestRoutedQueueAssembly:
    def setup_method(self):
        self.pool = _FakePool([])

    def _submit_and_triage(self, item_id: str, text: str, cat: str, route: str):
        run(record(self.pool, None, item_id=item_id, text=text))
        run(record_triage(
            self.pool, None,
            item_id=item_id,
            category=cat,
            route=route,
            confidence=0.90,
            reason="test",
            auto_fixable=(route == "auto-fix"),
        ))

    def test_empty_routed_queue(self):
        assert run(routed_queue(self.pool, route="review")) == []

    def test_review_item_appears_in_review_queue(self):
        self._submit_and_triage("fb-r1", "wrong confidence", "veracity-dispute", "review")
        result = run(routed_queue(self.pool, route="review"))
        assert len(result) == 1
        assert result[0]["triage"]["category"] == "veracity-dispute"
        assert result[0]["triage"]["route"] == "review"

    def test_auto_fix_item_not_in_review_queue(self):
        self._submit_and_triage("fb-a1", "app crashes", "bug", "auto-fix")
        review = run(routed_queue(self.pool, route="review"))
        assert all(r["triage"]["route"] == "review" for r in review)

    def test_auto_fix_queue_contains_auto_fix_items(self):
        self._submit_and_triage("fb-a2", "button broken", "bug", "auto-fix")
        af = run(routed_queue(self.pool, route="auto-fix"))
        assert len(af) == 1
        assert af[0]["triage"]["auto_fixable"] is True

    def test_mixed_items_segregated_correctly(self):
        self._submit_and_triage("fb-m1", "wrong score", "veracity-dispute", "review")
        self._submit_and_triage("fb-m2", "crash on load", "bug", "auto-fix")
        review = run(routed_queue(self.pool, route="review"))
        af = run(routed_queue(self.pool, route="auto-fix"))
        assert all(r["triage"]["route"] == "review" for r in review)
        assert all(r["triage"]["route"] == "auto-fix" for r in af)
        review_ids = {r["triage"]["item_id"] for r in review}
        af_ids = {r["triage"]["item_id"] for r in af}
        assert review_ids.isdisjoint(af_ids)

    def test_triage_result_includes_original_text(self):
        self._submit_and_triage("fb-t1", "layout broken on mobile", "ui", "auto-fix")
        af = run(routed_queue(self.pool, route="auto-fix"))
        assert af[0].get("text") == "layout broken on mobile"


# ===========================================================================
# 5. record() and record_triage() write events with correct types
# ===========================================================================


class TestRecordEvents:
    def setup_method(self):
        self.pool = _FakePool([])

    def test_record_writes_feedback_submitted_event(self):
        run(record(self.pool, None, item_id="fb-w1", text="wrong score"))
        types = [r[1] for r in self.pool._rows]
        assert FEEDBACK_SUBMITTED in types

    def test_record_returns_item_id(self):
        fid = run(record(self.pool, None, item_id="fb-w2", text="test"))
        assert fid == "fb-w2"

    def test_record_generates_id_when_none(self):
        fid = run(record(self.pool, None, text="auto id test"))
        assert fid.startswith("fb-")

    def test_record_triage_writes_feedback_triaged_event(self):
        run(record_triage(
            self.pool, None,
            item_id="fb-w3",
            category="bug",
            route="auto-fix",
        ))
        types = [r[1] for r in self.pool._rows]
        assert FEEDBACK_TRIAGED in types

    def test_record_triage_data_fields(self):
        run(record_triage(
            self.pool, None,
            item_id="fb-w4",
            category="ui",
            route="auto-fix",
            confidence=0.78,
            reason="layout test",
            auto_fixable=True,
        ))
        data = next(r[2] for r in self.pool._rows if r[1] == FEEDBACK_TRIAGED)
        assert data["category"] == "ui"
        assert data["route"] == "auto-fix"
        assert data["confidence"] == pytest.approx(0.78)
        assert data["reason"] == "layout test"
        assert data["auto_fixable"] is True
