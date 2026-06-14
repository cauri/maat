"""Deterministic pipeline tests (no live API — those stay out of the CI gate)."""

from maat.pipeline.claim import Claim
from maat.pipeline.classify import PROMPT as CLASSIFY_PROMPT
from maat.pipeline.extract import PROMPT as EXTRACT_PROMPT


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


def test_claim_carries_classification_fields():
    c = Claim.model_validate(
        {
            "text": "The deal will collapse next quarter",
            "voice": "own",
            "evidence_span": "the deal will collapse next quarter",
            "kind": "projection",
            "is_synthesis": False,
            "horizon": "next quarter",
        }
    )
    assert c.kind == "projection"
    assert c.horizon == "next quarter"


def test_classification_fields_default_unset():
    c = Claim.model_validate({"text": "x", "voice": "own", "evidence_span": "x"})
    assert c.kind is None
    assert c.is_synthesis is False
    assert c.horizon is None


def test_prompts_keep_context_placeholders():
    for token in ("{article_text}", "{source_metadata}", "{detected_language}"):
        assert token in EXTRACT_PROMPT
    for token in ("{article_text}", "{claims_json}"):
        assert token in CLASSIFY_PROMPT


def test_event_envelope_shape():
    import json

    from maat.events import envelope

    e = json.loads(envelope("art-1", "article.ingested", {"title": "x"}))
    assert e == {
        "stream_id": "art-1",
        "type": "article.ingested",
        "data": {"title": "x"},
        "tenant_id": "cauri",
    }


def test_claim_gets_default_id():
    c = Claim.model_validate({"text": "x", "voice": "own", "evidence_span": "x"})
    assert isinstance(c.id, str) and len(c.id) == 36
