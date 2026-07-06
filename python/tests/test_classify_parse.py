"""Tests for the classifier's result parsing (§5.3) — truncation salvage + padding (P14 fix).

Observed live: a claim-dense article's classification array truncated mid-object at the token
budget and the WHOLE classification (then the analysis) was lost. The parse now mirrors
extract._claim_objects (salvage complete objects) and pads to the claim count so results always
zip 1:1 — a claim may end up unclassified (kind=None), never silently dropped.
"""

from __future__ import annotations

import pytest

from maat.pipeline.classify import _classifications


def test_clean_array_parses_and_pads_nothing():
    raw = '```json\n[{"kind": "fact"}, {"kind": "projection"}]\n```'
    out = _classifications(raw, 2)
    assert [r.get("kind") for r in out] == ["fact", "projection"]


def test_truncated_array_salvages_complete_objects_and_pads():
    raw = '[\n  {"kind": "fact", "reason": "x"},\n  {"kind": "projection", "reason": "y"},\n  {"ki'
    out = _classifications(raw, 4)
    assert len(out) == 4  # padded — no claim is ever dropped
    assert out[0]["kind"] == "fact"
    assert out[1]["kind"] == "projection"
    assert out[2] == {} and out[3] == {}  # unclassified, not vanished


def test_overlong_array_is_clipped_to_claim_count():
    raw = '[{"kind": "fact"}, {"kind": "fact"}, {"kind": "fact"}]'
    assert len(_classifications(raw, 2)) == 2


def test_no_array_raises():
    with pytest.raises(ValueError, match="no JSON array"):
        _classifications("the model said something chatty", 3)


def test_garbage_after_bracket_raises():
    with pytest.raises(ValueError, match="no parseable"):
        _classifications("[ this is not json }", 2)
