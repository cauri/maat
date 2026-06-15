"""Tests for maat.metrics.de_us — de-US-centering metrics.

All tests are deterministic, pure, and require no DB or network.
Coverage matrix:
  - empty feed → all zeros
  - single source
  - US-dominated feed → low score
  - globally balanced feed → high score
  - per-axis functions: geographic_distribution, language_distribution,
    anglo_share, herfindahl, originator_country_count
  - custom targets
  - unknown / None metadata handling
  - score additivity and monotonicity
"""

from maat.metrics.de_us import (
    ANGLO_COUNTRIES,
    ScoreBreakdown,
    SourceMeta,
    Targets,
    anglo_share,
    geographic_distribution,
    herfindahl,
    language_distribution,
    originator_country_count,
    score,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _us_feed(n: int = 10) -> list[SourceMeta]:
    """n US English sources — as Anglo-dominated as it gets."""
    return [SourceMeta("US", "en")] * n


def _balanced_feed() -> list[SourceMeta]:
    """15 sources across 10 countries and 6 languages — globally representative."""
    return [
        SourceMeta("CN", "zh"),
        SourceMeta("IN", "hi"),
        SourceMeta("BR", "pt"),
        SourceMeta("NG", "ha"),
        SourceMeta("NG", "en"),
        SourceMeta("EG", "ar"),
        SourceMeta("ID", "id"),
        SourceMeta("JP", "ja"),
        SourceMeta("MX", "es"),
        SourceMeta("ZA", "zu"),
        SourceMeta("RU", "ru"),
        SourceMeta("TR", "tr"),
        SourceMeta("KE", "sw"),
        SourceMeta("DE", "de"),
        SourceMeta("TH", "th"),
    ]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_score_empty_feed_is_all_zeros():
    s = score([])
    assert s == ScoreBreakdown(
        anglo=0.0, concentration=0.0, country_diversity=0.0,
        language_diversity=0.0, language_dominance=0.0, overall=0.0,
    )


def test_score_empty_overall_is_zero():
    assert score([]).overall == 0.0


def test_geographic_distribution_empty():
    assert geographic_distribution([]) == {}


def test_language_distribution_empty():
    assert language_distribution([]) == {}


def test_anglo_share_empty():
    assert anglo_share([]) == 0.0


def test_herfindahl_empty():
    assert herfindahl([]) == 0.0


def test_originator_country_count_empty():
    assert originator_country_count([]) == 0


def test_single_us_source():
    sources = [SourceMeta("US", "en")]
    s = score(sources)
    assert s.overall < 0.5, "a single US-English source should score low"
    assert s.anglo == 0.0, "Anglo share of 1.0 (above 0.40 ceiling) → 0 credit"
    assert s.concentration == 0.0, "HHI=1.0 (full monopoly) → 0 credit"
    assert s.country_diversity < 1.0
    assert s.language_diversity < 1.0


def test_single_non_anglo_source():
    sources = [SourceMeta("NG", "ha")]
    s = score(sources)
    # no Anglo sources → full credit on anglo axis
    assert s.anglo == 1.0
    # still single-country → zero concentration credit
    assert s.concentration == 0.0


def test_all_unknown_metadata():
    sources = [SourceMeta(None, None)] * 5
    s = score(sources)
    # no country info → no Anglo sources detected → full Anglo credit (vacuously true)
    assert s.anglo == 1.0
    # no country info → no concentration can be measured → HHI=0 → full credit
    assert s.concentration == 1.0
    # no country info → zero distinct countries → zero country diversity
    assert s.country_diversity == 0.0
    # no language info → language dominance scores 0 (no data → no credit)
    assert s.language_dominance == 0.0


# ---------------------------------------------------------------------------
# US-dominated feed → low score
# ---------------------------------------------------------------------------


def test_us_dominated_feed_scores_low():
    sources = _us_feed(10)
    s = score(sources)
    assert s.overall < 0.3, f"US-only feed overall={s.overall}, expected < 0.3"
    assert s.anglo == 0.0, "100% Anglo share → zero credit on Anglo axis"
    assert s.concentration == 0.0, "100% one-country HHI → zero credit"
    assert s.language_diversity < 1.0, "single language → below diversity target"


def test_us_uk_dominated_also_scores_low():
    sources = [SourceMeta("US", "en")] * 5 + [SourceMeta("GB", "en")] * 5
    s = score(sources)
    assert s.overall < 0.3
    assert s.anglo == 0.0


def test_heavily_anglo_feed_scores_below_threshold():
    # 8 US + 2 non-Anglo — Anglo share = 0.80, well above 0.40 ceiling
    sources = [SourceMeta("US", "en")] * 8 + [SourceMeta("NG", "ha"), SourceMeta("BR", "pt")]
    s = score(sources)
    assert s.overall < 0.5
    assert s.anglo < 0.5


# ---------------------------------------------------------------------------
# Globally balanced feed → high score
# ---------------------------------------------------------------------------


def test_balanced_feed_scores_high():
    sources = _balanced_feed()
    s = score(sources)
    assert s.overall > 0.7, f"balanced feed overall={s.overall}, expected > 0.7"
    assert s.anglo == 1.0, "no Anglo sources → full credit"
    # HHI is low (≈0.08) which is below the 0.25 target → full concentration credit
    assert s.concentration == 1.0, "well-spread feed clears HHI target → full concentration credit"


def test_balanced_feed_all_axes_positive():
    s = score(_balanced_feed())
    assert s.anglo > 0.0
    assert s.concentration > 0.0
    assert s.country_diversity > 0.0
    assert s.language_diversity > 0.0
    assert s.language_dominance > 0.0


def test_ideal_feed_nearly_perfect():
    """A feed hitting all default targets should score close to 1.0."""
    # 10 countries, 5 languages, 0 Anglo, very spread
    sources = [
        SourceMeta("CN", "zh"), SourceMeta("IN", "hi"), SourceMeta("BR", "pt"),
        SourceMeta("NG", "ha"), SourceMeta("EG", "ar"), SourceMeta("ID", "id"),
        SourceMeta("JP", "ja"), SourceMeta("MX", "es"), SourceMeta("ZA", "zu"),
        SourceMeta("KE", "sw"),
    ]
    s = score(sources)
    assert s.overall >= 0.8, f"near-ideal feed overall={s.overall}"
    assert s.anglo == 1.0
    assert s.concentration >= 0.8


# ---------------------------------------------------------------------------
# Per-axis unit tests
# ---------------------------------------------------------------------------


def test_geographic_distribution_fractions_sum_to_one():
    sources = [SourceMeta("US", "en"), SourceMeta("NG", "ha"), SourceMeta("US", "en")]
    dist = geographic_distribution(sources)
    assert abs(sum(dist.values()) - 1.0) < 1e-4


def test_geographic_distribution_counts_correctly():
    sources = [SourceMeta("US", "en"), SourceMeta("NG", "ha"), SourceMeta("US", "en")]
    dist = geographic_distribution(sources)
    assert dist["US"] == round(2 / 3, 4)
    assert dist["NG"] == round(1 / 3, 4)


def test_geographic_distribution_excludes_unknown():
    sources = [SourceMeta("US", "en"), SourceMeta(None, "fr"), SourceMeta("US", "en")]
    dist = geographic_distribution(sources)
    assert "US" in dist
    assert None not in dist
    assert abs(dist["US"] - 1.0) < 1e-4  # the None entry is excluded from denominator


def test_language_distribution_fractions_sum_to_one():
    sources = [SourceMeta("US", "en"), SourceMeta("NG", "ha"), SourceMeta("BR", "pt")]
    dist = language_distribution(sources)
    assert abs(sum(dist.values()) - 1.0) < 1e-4


def test_language_distribution_lowercases_tags():
    sources = [SourceMeta("BR", "PT-BR"), SourceMeta("US", "en")]
    dist = language_distribution(sources)
    assert "pt-br" in dist
    assert "en" in dist


def test_language_distribution_excludes_unknown():
    sources = [SourceMeta("US", "en"), SourceMeta("NG", None)]
    dist = language_distribution(sources)
    assert len(dist) == 1
    assert "en" in dist


def test_anglo_share_us_only():
    assert anglo_share(_us_feed(5)) == 1.0


def test_anglo_share_no_anglo():
    sources = [SourceMeta("NG", "ha"), SourceMeta("CN", "zh")]
    assert anglo_share(sources) == 0.0


def test_anglo_share_mixed():
    sources = [SourceMeta("US", "en"), SourceMeta("GB", "en"), SourceMeta("NG", "ha")]
    # 2 Anglo out of 3 known
    assert anglo_share(sources) == round(2 / 3, 4)


def test_anglo_share_ignores_unknown_country():
    sources = [SourceMeta("US", "en"), SourceMeta(None, "ha")]
    assert anglo_share(sources) == 1.0  # 1/1 known source is Anglo


def test_anglo_countries_are_us_and_gb():
    assert "US" in ANGLO_COUNTRIES
    assert "GB" in ANGLO_COUNTRIES
    assert len(ANGLO_COUNTRIES) == 2


def test_herfindahl_single_country_is_one():
    assert herfindahl(_us_feed(5)) == 1.0


def test_herfindahl_two_equal_countries():
    sources = [SourceMeta("US", "en"), SourceMeta("NG", "ha")]
    # each 0.5 → HHI = 0.5^2 + 0.5^2 = 0.5
    assert herfindahl(sources) == 0.5


def test_herfindahl_decreases_with_more_countries():
    two = [SourceMeta("US", "en"), SourceMeta("NG", "ha")]
    four = [SourceMeta("US", "en"), SourceMeta("NG", "ha"),
            SourceMeta("CN", "zh"), SourceMeta("BR", "pt")]
    assert herfindahl(four) < herfindahl(two)


def test_herfindahl_no_known_country_is_zero():
    sources = [SourceMeta(None, "en"), SourceMeta(None, "fr")]
    assert herfindahl(sources) == 0.0


def test_originator_country_count_distinct():
    sources = [SourceMeta("US", "en"), SourceMeta("US", "es"), SourceMeta("NG", "ha")]
    assert originator_country_count(sources) == 2


def test_originator_country_count_excludes_none():
    sources = [SourceMeta("US", "en"), SourceMeta(None, "ha")]
    assert originator_country_count(sources) == 1


# ---------------------------------------------------------------------------
# Custom targets
# ---------------------------------------------------------------------------


def test_custom_targets_strict():
    """With very strict country/language targets, a small feed scores low on those axes."""
    sources = [SourceMeta("NG", "ha"), SourceMeta("BR", "pt"), SourceMeta("CN", "zh")]
    # 3 countries vs target 50 → country_diversity = 0.06; 3 languages vs 20 → 0.15
    strict = Targets(no_anglo_above=0.40, hhi_below=0.25, min_countries=50, min_languages=20)
    s = score(sources, targets=strict)
    assert s.country_diversity < 0.1, "3 countries vs 50 target → very low diversity credit"
    assert s.language_diversity < 0.2, "3 languages vs 20 target → very low language credit"
    assert s.overall < 0.7


def test_custom_targets_lenient():
    """With very lenient targets, even a US-only feed can score well on most axes."""
    sources = _us_feed(3)
    lenient = Targets(no_anglo_above=1.0, hhi_below=1.0, min_countries=1, min_languages=1)
    s = score(sources, targets=lenient)
    # Anglo share ≤ 1.0 ceiling → full credit; HHI ≤ 1.0 → full credit
    assert s.anglo == 1.0
    assert s.concentration == 1.0


def test_default_targets_are_reasonable():
    t = Targets()
    assert 0 < t.no_anglo_above < 1
    assert 0 < t.hhi_below < 1
    assert t.min_countries > 0
    assert t.min_languages > 0
    assert 0 < t.no_single_lang_above < 1


# ---------------------------------------------------------------------------
# Score monotonicity — adding more diverse sources should not decrease score
# ---------------------------------------------------------------------------


def test_adding_non_anglo_source_does_not_decrease_overall():
    base = [SourceMeta("US", "en")] * 5
    more = base + [SourceMeta("CN", "zh")]
    assert score(more).overall >= score(base).overall


def test_score_strictly_increases_from_us_only_to_balanced():
    s_low = score(_us_feed(10))
    s_high = score(_balanced_feed())
    assert s_high.overall > s_low.overall


def test_language_dominance_axis_penalises_english_monoculture():
    mono = [SourceMeta("NG", "en"), SourceMeta("IN", "en"), SourceMeta("CN", "en")] * 4
    mixed = [SourceMeta("NG", "ha"), SourceMeta("IN", "hi"), SourceMeta("CN", "zh"),
             SourceMeta("MX", "es"), SourceMeta("BR", "pt"), SourceMeta("EG", "ar")]
    assert score(mixed).language_dominance >= score(mono).language_dominance


# ---------------------------------------------------------------------------
# Score breakdown is a NamedTuple with the right fields
# ---------------------------------------------------------------------------


def test_score_returns_named_tuple_with_expected_fields():
    s = score([SourceMeta("NG", "ha")])
    assert hasattr(s, "anglo")
    assert hasattr(s, "concentration")
    assert hasattr(s, "country_diversity")
    assert hasattr(s, "language_diversity")
    assert hasattr(s, "language_dominance")
    assert hasattr(s, "overall")


def test_all_axis_scores_in_0_1():
    for sources in [[], _us_feed(5), _balanced_feed()]:
        s = score(sources)
        for val in s:
            assert 0.0 <= val <= 1.0, f"axis score {val} out of [0, 1] for sources={sources}"
