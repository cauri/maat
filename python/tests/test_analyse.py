"""Tests for the Analyse orchestrator (P14 #366) — corpus-inherit flow, offline seams.

Covers: matched claims fold the pasted article into the cluster (independent report counts,
wire reprint collapses), novel claims stand alone with honest wording, extremity is public,
"corroborated" is reserved for outside confirmation, projections split out, the disqualifying
central-claim rule reaches the overall score, and publisher reputation lookup.
"""

from __future__ import annotations

from maat.acquire.fetch import FetchedPage
from maat.pipeline.analyse import (
    ClaimReading,
    CorpusFact,
    analyse_article,
    claim_verdict,
    match_claims,
)
from maat.pipeline.claim import Claim
from maat.pipeline.corroborate import ClaimRow

# --- offline seams -------------------------------------------------------------------------

_PARIS = "The finance minister visited Paris on Tuesday"
_PARIS_CORPUS = "The minister visited Paris on Tuesday"
_GOLD = "The central bank secretly sold half its gold"
_FORECAST = "Analysts expect further meetings next month"

_VEC = {
    _PARIS: [1.0, 0.0, 0.0],
    _PARIS_CORPUS: [1.0, 0.0, 0.0],  # same fact → cosine 1.0
    _GOLD: [0.0, 1.0, 0.0],
}


def fake_embed(texts):
    return [_VEC.get(t, [0.0, 0.0, 1.0]) for t in texts]


def fake_extremity(text):
    return {_GOLD: "extraordinary"}[text]  # KeyError = extremity wrongly re-rated for a match


_BODY = (
    "On Tuesday the finance minister travelled to Paris for talks, according to the "
    "government's published schedule. The central bank secretly sold half its gold, an "
    "insider claimed. Analysts expect further meetings next month."
)


def fake_fetch(url):
    return FetchedPage(body=_BODY, title="Minister in Paris; gold questions swirl",
                       image=None, date="2026-07-06")


def _claims():
    return [
        Claim(text=_PARIS, voice="own", evidence_span=_PARIS),
        Claim(text=_GOLD, voice="attributed", speaker="an insider", in_headline=True,
              evidence_span=_GOLD),
        Claim(text=_FORECAST, voice="own", evidence_span=_FORECAST),
    ]


def fake_extract(body, **kw):
    return _claims()


def fake_classify(claims, **kw):
    kinds = {_FORECAST: "projection"}
    return [c.model_copy(update={"kind": kinds.get(c.text, "fact")}) for c in claims]


def corpus():
    members = [
        ClaimRow(id="m1", text=_PARIS_CORPUS, article_id="a1", source="reuters.com"),
        ClaimRow(id="m2", text="Minister's Paris visit confirmed for Tuesday",
                 article_id="a2", source="apnews.com"),
    ]
    bodies = {
        "a1": "The minister visited Paris on Tuesday, the foreign ministry said in a briefing.",
        "a2": "A government spokesperson said the minister's Tuesday visit to Paris went ahead.",
    }
    return [CorpusFact(cluster_id="cl1", fact=_PARIS_CORPUS, extremity="routine",
                       member_claims=members, bodies=bodies,
                       originator_sources=[["reuters.com"], ["apnews.com"]])]


def analyse(url="https://www.chronicle.example/paris-story", reputation=None, **kw):
    return analyse_article(
        url,
        corpus=kw.pop("corpus", corpus()),
        reputation=reputation or {},
        fetch=kw.pop("fetch", fake_fetch),
        extract=fake_extract,
        classify=fake_classify,
        extremity_of=fake_extremity,
        embed=fake_embed,
        language_of=lambda _t: "en",
        **kw,
    )


# --- full flow ------------------------------------------------------------------------------


def test_matched_claim_folds_pasted_article_into_cluster():
    res = analyse()
    paris = next(r for r in res.facts if r.claim.text == _PARIS)
    assert paris.matched_cluster_id == "cl1"
    # 2 corpus originators + this article (independent wording) = 3
    assert paris.independent_originators == 3
    assert paris.extremity == "routine"  # carried from the cluster, not re-rated
    assert paris.verdict.startswith(("Well corroborated", "Corroborated"))


def test_wire_reprint_collapses_not_double_counts():
    reprint_body = corpus()[0].bodies["a1"]  # near-verbatim of member a1

    def reprint_fetch(url):
        return FetchedPage(body=reprint_body, title=None, image=None, date=None)

    res = analyse(fetch=reprint_fetch)
    paris = next(r for r in res.facts if r.claim.text == _PARIS)
    assert paris.independent_originators == 2  # collapsed into a1's originator group


def test_novel_extraordinary_headline_claim_disqualifies_article():
    res = analyse()
    gold = next(r for r in res.facts if r.claim.text == _GOLD)
    assert gold.matched_cluster_id is None
    assert gold.extremity == "extraordinary"
    assert gold.independent_originators == 1
    assert gold.verdict == "Only this source — below the bar for a extraordinary claim"
    assert gold.tier == "floor"
    # ... and the central-claim rule reaches the overall score.
    assert res.score.band == "disqualified"
    assert res.score.label == "Fails verification"
    assert res.score.score <= 20


def test_projections_split_out_and_never_scored():
    res = analyse()
    assert [r.claim.text for r in res.projections] == [_FORECAST]
    assert res.projections[0].verdict == "Forecast / opinion — not scored for truth"
    assert all(r.claim.text != _FORECAST for r in res.facts)


def test_publisher_reputation_lookup():
    assert analyse().publisher_score is None  # not yet rated
    res = analyse(reputation={"chronicle.example": 0.81})
    assert res.publisher_score == 0.81


def test_unfetchable_url_raises_clearly():
    try:
        analyse(fetch=lambda _u: None)
    except ValueError as e:
        assert "could not extract" in str(e)
    else:
        raise AssertionError("expected ValueError")


# --- verdict wording (the reserved word) ----------------------------------------------------


def test_lone_routine_claim_never_says_corroborated():
    label, tier = claim_verdict(0.65, 1, False, "routine")
    assert label == "Only this source so far"
    assert tier == "mid"  # score unchanged — only the words are honest
    assert "corroborat" not in label.lower()


def test_lone_extraordinary_claim_names_the_bar():
    label, tier = claim_verdict(0.24, 1, False, "extraordinary")
    assert "below the bar" in label
    assert tier == "floor"


def test_disputed_overrides_everything():
    label, tier = claim_verdict(0.90, 4, True, "routine", disputed=True)
    assert label.startswith("Disputed")
    assert tier == "floor"


def test_outside_confirmation_uses_the_feed_ladder():
    assert claim_verdict(0.88, 3, False, "notable")[0] == "Well corroborated"
    assert claim_verdict(0.65, 2, False, "notable")[0] == "Corroborated"


def test_lone_primary_source_is_named():
    label, _tier = claim_verdict(0.83, 1, True, "notable")
    assert label == "Stated by the primary source"


# --- matching -------------------------------------------------------------------------------


def test_match_claims_threshold_and_misses():
    hits = match_claims([_PARIS, _GOLD], corpus(), embed=fake_embed, threshold=0.82)
    assert hits == [0, None]
    assert match_claims([], corpus(), embed=fake_embed) == []
    assert match_claims([_PARIS], [], embed=fake_embed) == [None]


def test_reading_shape_is_complete():
    res = analyse()
    r = res.facts[0]
    assert isinstance(r, ClaimReading)
    for f in ("extremity", "confidence", "independent_originators", "verdict", "tier"):
        assert getattr(r, f) is not None
