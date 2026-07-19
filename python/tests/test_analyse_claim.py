"""Tests for claim mode (P16 #450) — ``analyse_claim`` + claim scoring, offline.

The structural invariant under test everywhere: the READER IS NEVER AN ORIGINATOR. A typed claim
starts from zero evidence — zero stays an honest zero ("No independent support found", never
"Single source"), corroboration counts only outside evidence, and a corpus match inherits the
cluster's standing without folding the reader in.
"""

from __future__ import annotations

import pytest

from maat.learning.article_credibility import ArticleClaim
from maat.learning.claim_credibility import score_claims
from maat.pipeline.analyse import (
    PRIVATE_DECLINE,
    AnalyseError,
    Citation,
    CorpusFact,
    analyse_claim,
    claim_mode_verdict,
)
from maat.pipeline.claim import Claim
from maat.pipeline.claimify import NormalisedClaim, NormalisedInput
from maat.pipeline.corroborate import ClaimRow

_RUMOUR = "Elon Musk and Tim Cook agreed to merge SpaceX and Apple"
_VOTE = "Parliament postponed the budget vote"
# Quotes carry attribution ("said in a statement") so the S2 sourcing scan reads them as
# provenanced reporting, not bald assertions — the realistic well-sourced-coverage shape.
_BBC_QUOTE = ("Parliament has postponed the budget vote until next month, "
              "the speaker said in a statement")
_RTS_QUOTE = ("The budget vote was postponed by parliament on Tuesday, "
              "speaker Anna Novak said in a statement")


def _normalise_fact(text, *claims):
    """A normalise seam returning fixed claims (default: one public fact = the input)."""
    out = list(claims) or [NormalisedClaim(text=text, kind="fact", subject="public")]

    def normalise(_raw):
        return NormalisedInput(claims=out, language="en")

    return normalise


def _extremity(mapping, default="notable"):
    return lambda text: mapping.get(text, default)


def fake_nli(premise, hypothesis):
    """Entail when the pair shares the postponed-vote fact; contradict on the denial quote."""
    texts = (premise, hypothesis)
    if any("was not postponed" in t for t in texts):
        return ("contradiction", 0.95)
    if all(("postponed" in t) for t in texts):
        return ("entailment", 0.9)
    return ("neutral", 0.9)


def _no_fetch(_url):
    return None  # unfetchable pages are judged on NLI alone — the offline-test fast path


# --- verdict wording --------------------------------------------------------------------------


def test_claim_mode_verdict_zero_evidence_never_invents_a_source():
    label, tier = claim_mode_verdict(0.0, 0, False, "routine")
    assert label == "No independent support found yet"
    assert tier == "lo"
    label, tier = claim_mode_verdict(0.0, 0, False, "extraordinary")
    assert label.startswith("No credible support found")
    assert tier == "floor"
    assert "source" not in label.split("—")[0]  # nobody published it — don't say "Single source"


def test_claim_mode_verdict_refuted_wording():
    label, tier = claim_mode_verdict(0.2, 0, False, "notable", disputed=True)
    assert label == "Refuted — contradicted by independent reporting"
    assert tier == "floor"
    label, _ = claim_mode_verdict(0.2, 3, True, "notable", primary_contradicted=True)
    assert label == "Refuted — contradicted by the primary source"


def test_claim_mode_verdict_positive_ladder():
    assert claim_mode_verdict(0.5, 1, True, "notable")[0] == "Stated by the primary source"
    assert claim_mode_verdict(0.5, 1, False, "notable")[0] == "Reported by a single source so far"
    label, tier = claim_mode_verdict(0.9, 3, True, "notable")
    assert label == "Well corroborated"
    assert tier == "hi"


# --- zero evidence ----------------------------------------------------------------------------


def test_extraordinary_claim_with_no_evidence_reads_no_credible_support():
    result = analyse_claim(
        f"I heard {_RUMOUR}",
        reputation={},
        normalise=_normalise_fact(_RUMOUR),
        extremity_of=_extremity({_RUMOUR: "extraordinary"}),
        web_search=lambda texts, own, deep=False: [[] for _ in texts],
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert reading.independent_originators == 0
    assert reading.confidence == 0.0
    assert reading.tier == "floor"
    assert reading.verdict.startswith("No credible support found")
    assert result.score.band == "disqualified"
    assert result.score.label == "No credible support"
    assert result.live is not None and result.live.deep_searched == 1  # the deep pass still tried


def test_routine_claim_with_no_evidence_is_gentle_not_disqualified():
    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="routine"),
        web_search=lambda texts, own, deep=False: [[] for _ in texts],
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert reading.verdict == "No independent support found yet"
    assert result.score.band == "single"
    assert result.score.label == "No independent support found"


# --- live corroboration -----------------------------------------------------------------------


def _vote_citations(*, tier=0):
    return [
        Citation(url="https://bbc.com/vote", domain="bbc.com", quote=_BBC_QUOTE, tier=tier),
        Citation(url="https://rts.ch/vote", domain="rts.ch", quote=_RTS_QUOTE),
    ]


def test_corroborated_claim_counts_only_outside_evidence():
    result = analyse_claim(
        f"is it true that {_VOTE}?",
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=lambda texts, own, deep=False: [_vote_citations() for _ in texts],
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    # Two outlets — and ONLY two: the reader's typed statement added no originator.
    assert reading.independent_originators == 2
    assert reading.verdict in ("Corroborated", "Well corroborated")
    assert result.score.score > 0
    assert result.live is not None and result.live.web_corroborated == 1


def test_authority_contradiction_refutes_even_alongside_support():
    denial = Citation(url="https://parliament.gov/statement", domain="parliament.gov",
                      quote="The budget vote was not postponed", tier=1)

    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=lambda texts, own, deep=False: [_vote_citations() for _ in texts],
        authority_search=lambda texts, own, deep=False: [[denial] for _ in texts],
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert reading.primary_contradicted is True
    assert reading.verdict == "Refuted — contradicted by the primary source"
    assert result.score.band == "disqualified"
    assert result.score.label == "Refuted"
    assert "primary source" in " ".join(result.score.why)


def test_lone_contradiction_reads_refuted():
    denial = Citation(url="https://bbc.com/deny", domain="bbc.com",
                      quote="The budget vote was not postponed")
    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=lambda texts, own, deep=False: [[denial] for _ in texts],
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert reading.disputed is True
    assert reading.verdict == "Refuted — contradicted by independent reporting"
    assert result.score.label == "Refuted"


def test_first_pass_opens_at_the_deep_budget():
    seen = []

    def web_search(texts, own, deep=False):
        seen.append(deep)
        return [_vote_citations() for _ in texts]

    analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=web_search,
        nli=fake_nli,
        fetch=_no_fetch,
    )
    # #451: the reader asked about exactly this claim — no shallow-first economy to protect.
    assert seen == [True]


def test_deep_pass_rescues_a_lone_claim():
    calls = {"n": 0}

    def web_search(texts, own, deep=False):
        assert deep is True          # every claim-mode pass runs at the deep budget (#451)
        calls["n"] += 1
        if calls["n"] == 1:          # pass 1 finds nothing …
            return [[] for _ in texts]
        return [_vote_citations() for _ in texts]  # … the still-lone re-search rescues

    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=web_search,
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert calls["n"] == 2
    assert reading.independent_originators == 2
    assert result.live is not None
    assert result.live.deep_searched == 1
    assert result.live.deep_rescued == 1


# --- corpus inherit ---------------------------------------------------------------------------


def test_corpus_match_inherits_without_folding_the_reader_in():
    match = CorpusFact(
        cluster_id="cluster-1", fact=_VOTE, extremity="notable",
        member_claims=[
            ClaimRow(id="m1", text=_BBC_QUOTE, article_id="a1", source="bbc.com"),
            ClaimRow(id="m2", text=_RTS_QUOTE, article_id="a2", source="rts.ch"),
        ],
        bodies={"a1": _BBC_QUOTE, "a2": _RTS_QUOTE},
        originator_sources=[["bbc.com"], ["rts.ch"]],
    )
    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}),
        corpus_lookup=lambda texts: [match for _ in texts],
        web_search=lambda texts, own, deep=False: pytest.fail("matched claim must not search"),
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    # The cluster's two originators — the reader's typed claim did not become a third.
    assert reading.independent_originators == 2
    assert reading.matched_cluster_id == "cluster-1"


def test_unhydrated_corpus_match_falls_through_to_live():
    empty = CorpusFact(cluster_id="cluster-2", fact=_VOTE)  # no member_claims arrived
    searched = {"n": 0}

    def web_search(texts, own, deep=False):
        searched["n"] += 1
        return [_vote_citations() for _ in texts]

    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        corpus_lookup=lambda texts: [empty for _ in texts],
        web_search=web_search,
        nli=fake_nli,
        fetch=_no_fetch,
    )
    assert searched["n"] >= 1
    assert result.facts[0].independent_originators == 2


# --- decomposition + input handling -----------------------------------------------------------


def test_decomposed_claims_anchor_on_the_weakest():
    solid = NormalisedClaim(text=_VOTE, kind="fact", subject="public")
    wild = NormalisedClaim(text=_RUMOUR, kind="fact", subject="public")

    def web_search(texts, own, deep=False):
        return [_vote_citations() if t == _VOTE else [] for t in texts]

    result = analyse_claim(
        f"{_VOTE} and also {_RUMOUR}",
        reputation={},
        normalise=_normalise_fact("", solid, wild),
        extremity_of=_extremity({_RUMOUR: "extraordinary"}, default="notable"),
        web_search=web_search,
        nli=fake_nli,
        fetch=_no_fetch,
    )
    assert len(result.facts) == 2
    assert result.score.band == "disqualified"  # the unsupported extraordinary claim anchors
    assert any("no independent support" in r for r in result.score.why)


def test_private_individual_is_declined():
    private = NormalisedClaim(text="My neighbour John stole a car", kind="fact",
                              subject="private")
    with pytest.raises(AnalyseError) as e:
        analyse_claim(
            "my neighbour john stole a car",
            reputation={},
            normalise=_normalise_fact("", private),
        )
    assert str(e.value) == PRIVATE_DECLINE


def test_opinion_only_input_is_forecast_band_not_an_error():
    opinion = NormalisedClaim(text="The government is doing a terrible job", kind="opinion",
                              subject="public")
    result = analyse_claim(
        "the government is doing a terrible job",
        reputation={},
        normalise=_normalise_fact("", opinion),
    )
    assert result.facts == []
    assert len(result.projections) == 1
    assert result.projections[0].verdict == "Forecast / opinion — not scored for truth"
    assert result.score.forecast_only is True
    assert result.score.label == "Nothing to check — opinion or forecast"


def test_empty_input_is_a_user_facing_error():
    with pytest.raises(AnalyseError):
        analyse_claim("   ", reputation={})


def test_progress_understood_carries_the_canonical_display():
    events: list[tuple[str, dict]] = []
    analyse_claim(
        f"I heard {_VOTE}",
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="routine"),
        web_search=lambda texts, own, deep=False: [[] for _ in texts],
        fetch=_no_fetch,
        progress=lambda kind, data: events.append((kind, data)),
    )
    kinds = [k for k, _ in events]
    assert kinds[0] == "understood"
    understood = events[0][1]
    assert understood["display"] == f"{_VOTE}."
    assert understood["claims"] == [{"text": _VOTE, "kind": "fact"}]
    assert "searching" in kinds and "claim" in kinds and "scored" in kinds


# --- claim credibility wording ----------------------------------------------------------------


def test_score_claims_rewords_the_zero_band_and_why():
    score = score_claims([
        ArticleClaim(confidence=0.0, independent_originators=0, has_primary=False,
                     extremity="routine", central=True),
    ])
    assert score.label == "No independent support found"
    assert any(w.startswith("the claim:") for w in score.why)
    assert not any("central" in w for w in score.why)


def test_score_claims_keeps_single_source_label_when_one_source_exists():
    score = score_claims([
        ArticleClaim(confidence=0.15, independent_originators=1, has_primary=False,
                     extremity="routine", central=True),
    ])
    assert score.label == "Single source / unverified"


def test_score_claims_forecast_wording():
    score = score_claims([])
    assert score.forecast_only is True
    assert score.label == "Nothing to check — opinion or forecast"


# --- the evidence braid (#451) ----------------------------------------------------------------


def _fc(polarity_rating, *, claim_text=_VOTE, url="https://factcheck.afp.com/vote",
        publisher="AFP Fact Check", site="factcheck.afp.com"):
    from maat.acquire.factcheck import FactCheck, rating_polarity

    return FactCheck(
        claim_text=claim_text, claimant="viral posts", claim_date="2026-07-10",
        publisher=publisher, site=site, review_url=url,
        review_title="Checked", review_date="2026-07-11",
        rating=polarity_rating, polarity=rating_polarity(polarity_rating),
    )


def test_factcheck_false_refutes_even_when_outlets_carry_the_claim():
    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=lambda texts, own, deep=False: [_vote_citations() for _ in texts],
        fact_check=lambda texts, lang: [[_fc("False")] for _ in texts],
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert reading.disputed is True
    assert reading.verdict == "Refuted — contradicted by independent reporting"
    assert result.score.label == "Refuted"
    assert result.live is not None
    assert result.live.factcheck_checked is True
    assert result.live.factcheck_refuted == 1


def test_factcheck_true_is_one_more_independent_originator():
    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=lambda texts, own, deep=False: [_vote_citations() for _ in texts],
        fact_check=lambda texts, lang: [[_fc("True")] for _ in texts],
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert reading.independent_originators == 3      # bbc + rts + the fact-checker
    assert result.live.factcheck_supported == 1


def test_factcheck_of_a_different_claim_moves_nothing():
    other = _fc("False", claim_text="A completely unrelated statement about sports")
    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=lambda texts, own, deep=False: [_vote_citations() for _ in texts],
        fact_check=lambda texts, lang: [[other] for _ in texts],
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert reading.disputed is False                  # failed the same-fact NLI gate
    assert result.live.factcheck_hits == 1            # found, counted — just never trusted
    assert result.live.factcheck_refuted == 0


def test_disagreeing_factchecks_cancel_to_the_evidence_fold():
    checks = [_fc("False"),
              _fc("True", url="https://fullfact.org/vote", publisher="Full Fact",
                  site="fullfact.org")]
    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=lambda texts, own, deep=False: [_vote_citations() for _ in texts],
        fact_check=lambda texts, lang: [checks for _ in texts],
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    assert reading.disputed is False                  # genuinely contested is not "Refuted"
    assert reading.verdict != "Refuted — contradicted by independent reporting"
    assert result.live.factcheck_refuted == 1 and result.live.factcheck_supported == 1


def test_candidate_leg_braids_with_web_rows_not_as_fallback():
    from maat.pipeline.analyse import LiveCandidate

    bbc_only = [Citation(url="https://bbc.com/vote", domain="bbc.com", quote=_BBC_QUOTE)]
    rts_cand = LiveCandidate(url="https://rts.ch/vote", domain="rts.ch", title="t",
                             body=_RTS_QUOTE)

    def fake_extract(body, **kw):
        return [Claim(text=_VOTE, voice="own", evidence_span=body[:20])]

    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=lambda texts, own, deep=False: [bbc_only for _ in texts],
        search=lambda q: [rts_cand],
        extract=fake_extract,
        embed=lambda texts: [[1.0, 0.0] for _ in texts],   # every text = the same fact
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    # The candidate leg ran ALONGSIDE a successful web pass and its outlet merged in.
    assert reading.independent_originators == 2
    assert result.live.apify_fallbacks == 1


def test_deep_rescue_keeps_braided_factcheck_evidence():
    calls = {"n": 0}

    def web_search(texts, own, deep=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return [[] for _ in texts]
        return [_vote_citations() for _ in texts]

    result = analyse_claim(
        _VOTE,
        reputation={},
        normalise=_normalise_fact(_VOTE),
        extremity_of=_extremity({}, default="notable"),
        web_search=web_search,
        fact_check=lambda texts, lang: [[_fc("True")] for _ in texts],
        nli=fake_nli,
        fetch=_no_fetch,
    )
    (reading,) = result.facts
    # Pass 1: fact-checker only (1 originator). Deep rescue adds bbc+rts — and must KEEP the
    # fact-checker in the re-fold: 3, not 2.
    assert reading.independent_originators == 3
    assert result.live.deep_rescued == 1
