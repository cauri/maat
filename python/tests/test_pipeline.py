"""Deterministic pipeline tests (no live API — those stay out of the CI gate)."""

from maat.pipeline.claim import Claim
from maat.pipeline.extract import PROMPT


def test_claim_validates_attributed_with_chain():
    c = Claim.model_validate(
        {
            "text": "the U.S. violated its ceasefire with Iran",
            "voice": "attributed",
            "speaker": "Abbas Araghchi",
            "relay_chain": ["the outlet", "Abbas Araghchi"],
            "in_headline": False,
            "evidence_span": "Araghchi said the U.S. had already violated its ceasefire",
        }
    )
    assert c.voice == "attributed"
    assert c.speaker == "Abbas Araghchi"
    assert c.relay_chain == ["the outlet", "Abbas Araghchi"]


def test_claim_defaults_for_own_voice():
    c = Claim.model_validate(
        {"text": "Iran suspended talks", "voice": "own", "evidence_span": "Iran Suspends Talks"}
    )
    assert c.speaker is None
    assert c.relay_chain is None
    assert c.in_headline is False


def test_prompt_keeps_context_placeholders():
    for token in ("{article_text}", "{source_metadata}", "{detected_language}"):
        assert token in PROMPT
