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


def test_collapse_same_outlet_to_one_originator():
    from maat.pipeline.corroborate import collapse_originators

    # Two distinct articles from the SAME outlet are one originator, not two — an outlet is
    # not independent of itself (real GDELT data: one domain published several pieces).
    bodies = {
        "a1": "The central bank raised rates today in a widely expected quarter-point move.",
        "a2": "Separately, the bank sharply revised its inflation forecast upward for next year.",
    }
    sources = {"a1": "econotimes.com", "a2": "econotimes.com"}
    assert len(collapse_originators(["a1", "a2"], bodies, sources)) == 1


def test_is_primary_source():
    from maat.pipeline.corroborate import is_primary_source

    assert is_primary_source("Valoria Ministry of Finance (official statement)")
    assert is_primary_source("European Central Bank")  # issuer of its own rate decision
    assert is_primary_source("Federal Reserve")
    assert not is_primary_source("Daily Herald")
    assert not is_primary_source("AFP")  # a wire agency relays, it is not a primary source
    assert not is_primary_source("Reuters")


def test_has_provenance_flags_bald_assertions():
    from maat.pipeline.corroborate import has_provenance

    # good reporting states where it got the claim — even an unnamed source
    assert has_provenance("The minister resigned, the ministry said.")
    assert has_provenance("According to two officials, the talks collapsed.")
    assert has_provenance('A source told this paper, "the deal is off."')
    assert has_provenance("Leaked documents reviewed by the outlet show the sale.")
    # a bald assertion with no stated provenance
    assert not has_provenance("The central bank secretly sold its entire gold reserve last year.")


def test_attribution_gradient_named_anonymous_bald():
    from maat.pipeline.corroborate import attribution_weight

    # named person / organisation -> full weight
    assert attribution_weight("The minister, Jane Doe, said the talks failed.", "Daily Report") == 1.0
    assert attribution_weight("The Finance Ministry said the deal was signed.", "Daily Report") == 1.0
    # anonymous but stated -> middle
    w_anon = attribution_weight("Two sources familiar with the matter said the deal is off.", "Daily Report")
    assert 0.3 < w_anon < 1.0
    # bald assertion, no attribution -> least
    assert attribution_weight("The deal is off.", "Random Blog") == 0.3
    # a primary source is its own provenance — full, even with a bald body
    assert attribution_weight("Rates go up a quarter point.", "Federal Reserve") == 1.0
    # the gradient orders strictly: named > anonymous > none
    assert attribution_weight("The deal is off.", "Random Blog") < w_anon < 1.0


def test_effective_originators_weights_by_sourcing():
    from maat.pipeline.corroborate import effective_originators

    bodies = {
        "named": "X happened, the Finance Ministry said in a statement.",
        "anon": "X happened, two sources familiar with the talks said.",
        "bald": "X happened.",
        "primary": "We are raising rates by a quarter point.",
    }
    sources = {"named": "Daily Report", "anon": "Weekly", "bald": "Random Blog", "primary": "Federal Reserve"}
    # named (1.0) + bald (0.3) -> 1.3
    assert effective_originators([["named"], ["bald"]], bodies, sources) == 1.3
    # named + anonymous (0.6) -> 1.6, between 1 and 2
    assert effective_originators([["named"], ["anon"]], bodies, sources) == 1.6
    # two fully-attributed originators -> 2.0
    assert effective_originators([["named"], ["primary"]], bodies, sources) == 2.0


def test_confidence_read_rises_with_corroboration_and_primary():
    from maat.pipeline.corroborate import confidence_read

    # diminishing returns on independent originators (default prior "notable", decay 0.55)
    assert confidence_read(1, False) == 0.45
    assert confidence_read(2, False) == 0.7
    assert confidence_read(3, False) < confidence_read(4, False)
    # a primary source closes half the remaining gap, never reaching certainty
    assert confidence_read(3, True) > confidence_read(3, False)
    assert confidence_read(9, True) <= 0.97
    # a single uncorroborated originator stays low
    assert confidence_read(1, False) < confidence_read(2, True)


def test_agglomerate_resists_single_link_chaining():
    from maat.pipeline.corroborate import _agglomerate

    # A~B and B~C strongly, but A and C are dissimilar. Single-linkage / connected
    # components would chain all three into one cluster (#20); average-linkage must not.
    sim = [
        [1.00, 0.86, 0.50],
        [0.86, 1.00, 0.86],
        [0.50, 0.86, 1.00],
    ]
    groups = _agglomerate(sim, 0.82)
    assert len(groups) == 2  # the bridge (B) does not drag A and C together
    assert not any(0 in g and 2 in g for g in groups)


def test_agglomerate_keeps_tight_cluster_together():
    from maat.pipeline.corroborate import _agglomerate

    sim = [[1.00, 0.91, 0.90], [0.91, 1.00, 0.92], [0.90, 0.92, 1.00]]
    assert [sorted(g) for g in _agglomerate(sim, 0.82)] == [[0, 1, 2]]


def test_agglomerate_separates_distinct_groups():
    from maat.pipeline.corroborate import _agglomerate

    # two tight pairs, far apart — must stay two clusters
    sim = [
        [1.0, 0.90, 0.10, 0.10],
        [0.90, 1.0, 0.10, 0.10],
        [0.10, 0.10, 1.0, 0.90],
        [0.10, 0.10, 0.90, 1.0],
    ]
    assert sorted(sorted(g) for g in _agglomerate(sim, 0.82)) == [[0, 1], [2, 3]]


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


def test_extremity_is_five_point_scale():
    from maat.pipeline.corroborate import confidence_read
    from maat.pipeline.extremity import LEVELS, _parse_extremity

    assert LEVELS == ("routine", "ordinary", "notable", "significant", "extraordinary")
    assert _parse_extremity('{"extremity": "significant"}') == "significant"
    assert _parse_extremity('{"extremity": "routine"}') == "routine"
    # confidence is strictly decreasing as the prior rises across all five levels
    confs = [confidence_read(3, False, lv) for lv in LEVELS]
    assert confs == sorted(confs, reverse=True)
    # the raised bar: an extraordinary claim earns much less than a routine one from 2 originators
    assert confidence_read(2, False, "routine") > confidence_read(2, False, "extraordinary") + 0.2


def test_admin_event_payload():
    from maat.events import admin_event

    d = admin_event("claim-1", reason="wrong axis", kind="fact")
    assert d == {"target": "claim-1", "actor": "operator", "reason": "wrong axis", "kind": "fact"}


def test_cluster_id_stable_and_order_independent():
    from maat.pipeline.corroborate import cluster_id

    assert cluster_id(["b", "a"]) == cluster_id(["a", "b"])  # order-independent
    assert cluster_id(["a", "b"]) != cluster_id(["a", "c"])  # membership-sensitive


def test_corroborate_fixed_collapses_wire_then_reads_confidence():
    # F3 recompute: an operator-fixed claim set is read WITHOUT re-clustering. The wire pair
    # collapses to one originator; the independent paper is the second. No LLM, no DB.
    from maat.pipeline.corroborate import ClaimRow, confidence_read, corroborate_fixed

    bodies = {
        "afp": "Minister X resigned on Tuesday amid a procurement scandal, the ministry said.",
        "reprint": "Minister X resigned on Tuesday amid a procurement scandal, the ministry said.",
        "indie": "After our shell-company investigation, X stepped down today, this paper found.",
    }
    claims = [
        ClaimRow(id="11111111-1111-1111-1111-111111111111", text="X resigned", article_id="afp", source="AFP"),
        ClaimRow(id="22222222-2222-2222-2222-222222222222", text="X resigned", article_id="reprint", source="Daily News"),
        ClaimRow(id="33333333-3333-3333-3333-333333333333", text="X resigned", article_id="indie", source="The Investigative Times"),
    ]
    corr = corroborate_fixed(claims, bodies, "notable")
    assert corr.independent_originators == 2  # wire reprint collapsed onto AFP
    assert not corr.has_primary
    assert corr.confidence == confidence_read(2, False, "notable")


def test_corroborate_fixed_honours_primary_and_carried_extremity():
    from maat.pipeline.corroborate import ClaimRow, confidence_read, corroborate_fixed

    bodies = {"a": "The ECB raised rates today.", "b": "Separately, the bank moved rates, our desk confirms."}
    claims = [
        ClaimRow(id="aaaaaaaa-0000-0000-0000-000000000001", text="rates up", article_id="a", source="European Central Bank"),
        ClaimRow(id="bbbbbbbb-0000-0000-0000-000000000002", text="rates up", article_id="b", source="Daily Herald"),
    ]
    corr = corroborate_fixed(claims, bodies, "extraordinary")
    assert corr.has_primary  # the ECB is the primary source for its own rate decision
    assert corr.extremity == "extraordinary"  # carried over, not re-rated
    assert corr.confidence == confidence_read(2, True, "extraordinary")
