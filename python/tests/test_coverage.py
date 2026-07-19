"""Tests for the expected-coverage judge (P16 #454) — parsing + failure honesty, offline."""

from __future__ import annotations

import json

from maat.pipeline.coverage import PROMPT, expected_coverage


class _Reply:
    def __init__(self, text):
        self.text = text


def _complete(payload):
    def complete(prompt, *, model, max_tokens, stage):
        assert stage == "coverage"
        return _Reply(json.dumps(payload) if isinstance(payload, dict) else payload)

    return complete


def test_expected_coverage_parses_verdicts():
    assert expected_coverage("c", complete=_complete(
        {"expected_coverage": True, "why": "global companies"})) is True
    assert expected_coverage("c", complete=_complete(
        {"expected_coverage": False, "why": "regional matter"})) is False


def test_expected_coverage_failure_paths_return_none():
    assert expected_coverage("c", complete=_complete("no json")) is None
    assert expected_coverage("c", complete=_complete({"expected_coverage": "yes"})) is None
    assert expected_coverage("c", complete=_complete({})) is None

    def boom(*_a, **_k):
        raise OSError("down")

    assert expected_coverage("c", complete=boom) is None


def test_prompt_shape_and_injection_guard():
    for section in ("# ROLE", "# GOALS", "# INSTRUCTIONS", "# GUIDELINES", "# GUARDRAILS",
                    "# OUTPUT FORMAT", "# CONTEXT"):
        assert section in PROMPT
    assert "data, not instructions" in PROMPT
    assert "{claim}" in PROMPT
