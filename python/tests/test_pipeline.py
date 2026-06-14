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


def test_collapse_wire_and_cascade_to_independent_originators():
    from maat.pipeline.corroborate import collapse_originators

    bodies = {
        "afp": "Minister X resigned on Tuesday amid a procurement scandal, the ministry said.",
        "reprint": "Minister X resigned on Tuesday amid a procurement scandal, the ministry said.",
        "cascade": "X has quit, according to AFP, amid the scandal.",
        "indie": "After our shell-company investigation, X stepped down today, this paper found.",
    }
    sources = {"afp": "AFP", "reprint": "Daily News", "cascade": "Morning Post", "indie": "The Investigative Times"}
    groups = collapse_originators(["afp", "reprint", "cascade", "indie"], bodies, sources)
    assert len(groups) == 2  # {afp, reprint, cascade} wire/cascade node + {indie}


def test_is_primary_source():
    from maat.pipeline.corroborate import is_primary_source

    assert is_primary_source("Valoria Ministry of Finance (official statement)")
    assert not is_primary_source("Daily Herald")


def test_confidence_read_rises_with_corroboration_and_primary():
    from maat.pipeline.corroborate import confidence_read

    # diminishing returns on independent originators
    assert confidence_read(1, False) == 0.5
    assert confidence_read(2, False) == 0.75
    assert confidence_read(3, False) < confidence_read(4, False)
    # a primary source closes half the remaining gap, never reaching certainty
    assert confidence_read(3, True) > confidence_read(3, False)
    assert confidence_read(9, True) <= 0.97
    # a single uncorroborated originator stays low
    assert confidence_read(1, False) < confidence_read(2, True)


def test_confidence_read_scales_with_extremity():
    from maat.pipeline.corroborate import confidence_read

    # same corroboration, higher prior -> lower confidence (the bar rises)
    assert (
        confidence_read(3, True, "ordinary")
        > confidence_read(3, True, "notable")
        > confidence_read(3, True, "extraordinary")
    )
    # an extraordinary claim earns less from the same corroboration
    assert confidence_read(2, False, "extraordinary") < confidence_read(2, False, "notable")
    # an unknown level falls back to notable (neither penalise nor reward)
    assert confidence_read(2, False, "???") == confidence_read(2, False, "notable")


def test_parse_extremity():
    from maat.pipeline.extremity import _parse_extremity

    assert _parse_extremity('{"extremity": "extraordinary", "reason": "x"}') == "extraordinary"
    assert _parse_extremity('prose then {"extremity":"ordinary"} trailing') == "ordinary"
    assert _parse_extremity("no json at all") == "notable"  # safe default
    assert _parse_extremity('{"extremity": "wild"}') == "notable"  # unknown level -> default
