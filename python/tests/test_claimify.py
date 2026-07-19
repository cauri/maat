"""Tests for the claim normaliser (P16 #450) — schema enforcement, offline.

The reply contract is the defence: claim mode has no page to verify against (no span guard), so
the fixed JSON schema — closed label sets, bounded count and length — must REJECT anything that
doesn't match, never repair it into something plausible.
"""

from __future__ import annotations

import json

import pytest

from maat.pipeline.claimify import (
    MAX_CLAIMS,
    PROMPT,
    ClaimifyError,
    NormalisedClaim,
    NormalisedInput,
    normalise_input,
)


class _Reply:
    def __init__(self, text: str) -> None:
        self.text = text


def _complete_returning(text: str):
    def complete(prompt, *, model, max_tokens, stage):
        assert stage == "claimify"
        complete.prompt = prompt  # captured for prompt-fill assertions
        return _Reply(text)

    return complete


def _reply(claims, language="en") -> str:
    return json.dumps({"language": language, "claims": claims})


# --- happy path -------------------------------------------------------------------------------


def test_hearsay_normalises_to_declarative_claims():
    complete = _complete_returning(_reply([
        {"text": "Elon Musk and Tim Cook agreed to merge SpaceX and Apple",
         "kind": "fact", "subject": "public"},
    ]))
    out = normalise_input(
        "I heard that Elon Musk and Tim Cook made a deal to merge SpaceX and Apple",
        complete=complete,
    )
    assert out.language == "en"
    assert [c.kind for c in out.claims] == ["fact"]
    assert out.claims[0].subject == "public"
    # the reader's raw wording went into the prompt as data, delimited
    assert "I heard that" in complete.prompt
    assert "<<<" in complete.prompt and ">>>" in complete.prompt
    assert "{reader_input}" not in complete.prompt
    assert "{max_claims}" not in complete.prompt


def test_display_is_the_canonical_claims_with_full_stops():
    out = NormalisedInput(
        claims=[NormalisedClaim("The vote was postponed", "fact", "public"),
                NormalisedClaim("Turnout hit a record high.", "fact", "public")],
        language="en",
    )
    assert out.display == "The vote was postponed. Turnout hit a record high."


def test_json_extracted_from_prose_and_fences():
    complete = _complete_returning(
        "Here you go:\n```json\n"
        + _reply([{"text": "X happened", "kind": "fact", "subject": "none"}])
        + "\n```"
    )
    out = normalise_input("did X happen?", complete=complete)
    assert out.claims[0].text == "X happened"


def test_duplicate_claims_dedupe_and_cap_at_max():
    claims = [{"text": f"Claim number {i}", "kind": "fact", "subject": "none"}
              for i in range(MAX_CLAIMS + 2)]
    claims.insert(1, {"text": "claim number 0", "kind": "fact", "subject": "none"})  # casefold dup
    out = normalise_input("many things", complete=_complete_returning(_reply(claims)))
    assert len(out.claims) == MAX_CLAIMS
    assert [c.text for c in out.claims] == ["Claim number 0", "Claim number 1", "Claim number 2"]


def test_language_defaults_to_unknown_when_missing():
    complete = _complete_returning(json.dumps(
        {"claims": [{"text": "X", "kind": "opinion", "subject": "none"}]}
    ))
    assert normalise_input("x", complete=complete).language == "unknown"


def test_overlong_claim_text_is_bounded():
    complete = _complete_returning(_reply([
        {"text": "word " * 200, "kind": "fact", "subject": "none"},
    ]))
    out = normalise_input("long", complete=complete)
    assert len(out.claims[0].text) <= 300


# --- schema rejection (never repair) ----------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "no json here at all",
    "[]",
    json.dumps({"language": "en", "claims": []}),
    json.dumps({"language": "en"}),
    json.dumps({"language": "en", "claims": ["a bare string"]}),
    json.dumps({"language": "en", "claims": [{"text": "", "kind": "fact", "subject": "none"}]}),
    json.dumps({"language": "en",
                "claims": [{"text": "X", "kind": "rumour", "subject": "none"}]}),
    json.dumps({"language": "en",
                "claims": [{"text": "X", "kind": "fact", "subject": "everyone"}]}),
    json.dumps({"language": "en", "claims": [{"kind": "fact", "subject": "none"}]}),
])
def test_schema_violations_reject_with_user_facing_message(bad):
    with pytest.raises(ClaimifyError) as e:
        normalise_input("whatever", complete=_complete_returning(bad))
    assert "checkable claim" in str(e.value)  # safe to show the reader verbatim


# --- the prompt seed --------------------------------------------------------------------------


def test_prompt_treats_input_as_data_and_follows_template():
    # cauri's template shape (docs/prompt-template.md) + the injection guardrail are load-bearing.
    for section in ("# ROLE", "# GOALS", "# PROCESS", "# GUIDELINES", "# GUARDRAILS",
                    "# OUTPUT FORMAT", "# CONTEXT"):
        assert section in PROMPT
    assert "data, not instructions" in PROMPT
    assert "{reader_input}" in PROMPT and "{max_claims}" in PROMPT
