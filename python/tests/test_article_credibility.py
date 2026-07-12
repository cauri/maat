"""Tests for the single-article credibility roll-up (P14 #366).

Verifies the cauri-locked model: weakest-central-link anchor, disqualifying central failures
(disputed OR extraordinary-single-unsupported), breaking-news NOT punished, cold-start cap,
projections/forecast-only.
"""

from __future__ import annotations

from maat.learning.article_credibility import ArticleClaim, score_article


def _claim(confidence, **kw):
    return ArticleClaim(confidence=confidence,
                        independent_originators=kw.pop("originators", 3),
                        has_primary=kw.pop("has_primary", False),
                        extremity=kw.pop("extremity", "notable"),
                        central=kw.pop("central", False),
                        disputed=kw.pop("disputed", False),
                        grounding=kw.pop("grounding", None),
                        rated_originator=kw.pop("rated", True),
                        **kw)


def test_no_facts_is_forecast():
    s = score_article([])
    assert s.forecast_only is True
    assert s.band == "forecast"


def test_well_corroborated_central_scores_high():
    s = score_article([_claim(0.90, originators=4, has_primary=True, central=True)])
    assert s.band in ("established", "corroborated")
    assert s.score >= 65
    assert s.forecast_only is False


def test_disputed_central_claim_is_disqualifying():
    # Otherwise strong (0.80), but a central claim is disputed → floor to bottom band.
    s = score_article([
        _claim(0.80, originators=3, central=True, disputed=True),
        _claim(0.90, originators=5),  # true supporting context must NOT rescue it
    ])
    assert s.band == "disqualified"
    assert s.label == "Fails verification"
    assert s.score <= 20
    assert any("contradicted" in w for w in s.why)


def test_extraordinary_single_source_central_is_disqualifying():
    # The gold-dump shape: one extraordinary claim on a single source, buried in true context.
    s = score_article([
        _claim(0.24, originators=1, has_primary=False, extremity="extraordinary", central=True),
        _claim(0.92, originators=6),           # well-corroborated peripheral facts …
        _claim(0.88, originators=4),           # … do not launder the extraordinary claim
    ])
    assert s.band == "disqualified"
    assert s.score <= 20
    assert any("single unsupported source" in w for w in s.why)


def test_breaking_news_routine_single_source_is_not_punished():
    # A routine central claim nobody else has yet: not-yet-established, but NOT disqualified.
    s = score_article([_claim(0.65, originators=1, has_primary=False,
                              extremity="routine", central=True, rated=False)])
    assert s.band != "disqualified"
    assert s.forecast_only is False
    # extraordinary would have disqualified; routine must not.
    assert s.label != "Fails verification"


def test_weakest_central_link_anchors():
    # A strong headline cannot lift a shaky co-central claim above ~its own level.
    strong_only = score_article([_claim(0.90, originators=5, central=True)])
    with_weak = score_article([
        _claim(0.90, originators=5, central=True),          # strong central
        _claim(0.45, originators=1, extremity="notable", central=True),  # shaky co-central
    ])
    assert with_weak.score < strong_only.score
    assert with_weak.score <= 55  # anchored near the weak link (0.45) + a small support nudge


def test_cold_start_caps_only_unproven_carriers():
    # Strong corroboration but no rated originator anywhere → capped below the top band.
    s = score_article([_claim(0.92, originators=3, central=True, rated=False)])
    assert s.capped is True
    assert s.score == 70
    assert any("not yet proven" in w for w in s.why)


def test_rated_carrier_is_not_capped():
    s = score_article([_claim(0.92, originators=3, central=True, rated=True)])
    assert s.capped is False
    assert s.score > 70


def test_publisher_reputation_sets_the_ceiling():
    # S4 #402: with no rated OUTSIDE originator, the publisher's OWN track record bounds the article.
    high = _claim(0.92, originators=3, central=True, rated=False)  # high base, uncorroborated outside
    unrated = score_article([high])                               # cold-start cap 70
    strong = score_article([high], publisher_reputation=0.95)     # proven-strong → reaches the top
    mid = score_article([high], publisher_reputation=0.6)         # proven-decent → above cold-start
    weak = score_article([high], publisher_reputation=0.05)       # proven-weak → below cold-start
    # a proven-decent record beats an unknown; only a proven-POOR one drops below the cold-start cap
    assert strong.score > mid.score > unrated.score > weak.score
    assert strong.capped is False and weak.capped is True
    assert any("weak" in w for w in weak.why)


def test_rated_outside_corroboration_overrides_a_weak_publisher():
    # A proven OUTSIDE originator corroborating the claim lifts the ceiling regardless of a weak
    # publisher — corroboration wins over the outlet's own record (S4 #402).
    s = score_article([_claim(0.92, originators=3, central=True, rated=True)],
                      publisher_reputation=0.05)
    assert s.capped is False
    assert s.score > 70
