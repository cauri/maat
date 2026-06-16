"""Backfill prior + archive-bias correction tests (P3, §8).

Covers:
  - strata_distribution: correct counts, missing-field fallbacks
  - correction_weights: IPW formula, normalisation, ordering, single-stratum edge case
  - bias_summary: over-represented detection, entropy improves, ESS bounds, raises on empty
  - End-to-end: 80% US-English skew is detected and corrected toward balance
No DB, no I/O — pure functions over synthetic data.
"""

import math

import pytest

from maat.learning.backfill import (
    BiasReport,
    StratumInfo,
    bias_summary,
    cap_per_stratum,
    correction_weights,
    strata_distribution,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _art(language: str, country: str, **extra) -> dict:
    return {"language": language, "country": country, **extra}


def _make_skewed_corpus(n_en_us: int, n_other: int, other_lang: str = "fr",
                         other_country: str = "fr") -> list[dict]:
    """A corpus dominated by English/US articles, with a tail of ``other`` strata."""
    corpus = [_art("en", "us") for _ in range(n_en_us)]
    # split the minority evenly across 3 other strata
    per_other = n_other // 3
    remainder = n_other - per_other * 3
    corpus += [_art(other_lang, other_country) for _ in range(per_other)]
    corpus += [_art("de", "de") for _ in range(per_other)]
    corpus += [_art("ar", "eg") for _ in range(per_other + remainder)]
    return corpus


# ---------------------------------------------------------------------------
# strata_distribution
# ---------------------------------------------------------------------------

class TestStrataDistribution:
    def test_empty_returns_empty(self):
        assert strata_distribution([]) == {}

    def test_single_article(self):
        dist = strata_distribution([_art("en", "us")])
        assert dist == {("en", "us"): 1}

    def test_counts_multiple_strata(self):
        arts = [_art("en", "us")] * 3 + [_art("fr", "fr")] * 2 + [_art("de", "de")]
        dist = strata_distribution(arts)
        assert dist[("en", "us")] == 3
        assert dist[("fr", "fr")] == 2
        assert dist[("de", "de")] == 1

    def test_case_and_whitespace_normalised(self):
        arts = [
            {"language": "EN", "country": "US"},
            {"language": " en ", "country": "us"},
            {"language": "en", "country": "US"},
        ]
        dist = strata_distribution(arts)
        assert dist == {("en", "us"): 3}

    def test_missing_fields_fallback_to_unknown(self):
        arts = [
            {},
            {"language": None},
            {"country": None},
            {"language": "", "country": ""},
        ]
        dist = strata_distribution(arts)
        assert dist == {("unknown", "unknown"): 4}

    def test_partial_fields(self):
        arts = [
            {"language": "en"},          # no country → unknown
            {"country": "de"},            # no language → unknown
        ]
        dist = strata_distribution(arts)
        assert dist[("en", "unknown")] == 1
        assert dist[("unknown", "de")] == 1

    def test_extra_fields_ignored(self):
        arts = [_art("en", "us", source="Reuters", date="2020-01-01", score=0.9)]
        dist = strata_distribution(arts)
        assert dist == {("en", "us"): 1}

    def test_total_count_equals_n_articles(self):
        arts = [_art("en", "us")] * 5 + [_art("fr", "fr")] * 3
        dist = strata_distribution(arts)
        assert sum(dist.values()) == 8


# ---------------------------------------------------------------------------
# correction_weights
# ---------------------------------------------------------------------------

class TestCorrectionWeights:
    def test_empty_returns_empty(self):
        assert correction_weights([]) == []

    def test_single_article_weight_equals_1(self):
        """One article, one stratum — balanced by definition, weight = 1."""
        ws = correction_weights([_art("en", "us")])
        assert len(ws) == 1
        assert abs(ws[0] - 1.0) < 1e-9

    def test_single_stratum_all_weights_equal_1(self):
        """When all articles are in the same stratum, every weight is 1."""
        arts = [_art("en", "us") for _ in range(10)]
        ws = correction_weights(arts)
        assert all(abs(w - 1.0) < 1e-9 for w in ws)

    def test_weights_sum_to_n(self):
        """Normalisation invariant: Σ w = n_articles."""
        arts = [_art("en", "us")] * 8 + [_art("fr", "fr")] * 2
        ws = correction_weights(arts)
        assert abs(sum(ws) - 10) < 1e-9

    def test_minority_stratum_gets_higher_weight(self):
        """Under-represented strata must receive weight > 1."""
        arts = [_art("en", "us")] * 8 + [_art("fr", "fr")] * 2
        ws = correction_weights(arts)
        # en/us articles (idx 0–7)
        en_w = ws[0]
        # fr/fr articles (idx 8–9)
        fr_w = ws[8]
        assert fr_w > en_w, "minority stratum must receive higher weight than majority"

    def test_dominant_stratum_downweighted(self):
        """Articles in the over-represented stratum must have weight < 1."""
        arts = [_art("en", "us")] * 8 + [_art("fr", "fr")] * 2
        ws = correction_weights(arts)
        # 2 strata → target = 0.5; en/us has 0.8, so its raw IPW < 1
        assert ws[0] < 1.0

    def test_balanced_corpus_all_weights_equal_1(self):
        """Perfectly balanced input: all weights should be exactly 1."""
        arts = [_art("en", "us")] * 5 + [_art("fr", "fr")] * 5
        ws = correction_weights(arts)
        assert all(abs(w - 1.0) < 1e-9 for w in ws)

    def test_length_matches_input(self):
        arts = [_art("en", "us")] * 7 + [_art("de", "de")] * 3
        ws = correction_weights(arts)
        assert len(ws) == 10

    def test_all_weights_positive(self):
        arts = _make_skewed_corpus(80, 20)
        ws = correction_weights(arts)
        assert all(w > 0 for w in ws)

    def test_ipw_formula_matches_manual_calculation(self):
        """Verify the raw formula: n_total / (n_strata * count), then normalised."""
        # 6 en/us, 2 fr/fr, 2 de/de  → n_total=10, n_strata=3
        arts = [_art("en", "us")] * 6 + [_art("fr", "fr")] * 2 + [_art("de", "de")] * 2
        ws = correction_weights(arts)

        # raw IPW
        raw_en = 10 / (3 * 6)   # ≈ 0.5556
        raw_fr = 10 / (3 * 2)   # ≈ 1.6667
        raw_de = 10 / (3 * 2)   # ≈ 1.6667
        raw_sum = 6 * raw_en + 2 * raw_fr + 2 * raw_de
        scale = 10 / raw_sum

        expected_en = raw_en * scale
        expected_fr = raw_fr * scale

        assert abs(ws[0] - expected_en) < 1e-9
        assert abs(ws[6] - expected_fr) < 1e-9


# ---------------------------------------------------------------------------
# bias_summary
# ---------------------------------------------------------------------------

class TestBiasSummary:
    def test_raises_on_empty(self):
        with pytest.raises(ValueError, match="at least one"):
            bias_summary([])

    def test_single_stratum(self):
        arts = [_art("en", "us")] * 5
        report = bias_summary(arts)
        assert report.n_articles == 5
        assert report.n_strata == 1
        assert report.most_overrepresented == ("en", "us")
        assert report.most_overrepresented_fraction == pytest.approx(1.0, abs=1e-6)
        # single stratum → entropy = 0 (it's the only cell)
        assert report.entropy_raw == pytest.approx(0.0, abs=1e-6)
        # ESS collapses: all weights equal 1, so ESS = n
        assert report.effective_sample_size == pytest.approx(5.0, abs=1e-3)

    def test_most_overrepresented_is_dominant_stratum(self):
        arts = [_art("en", "us")] * 8 + [_art("fr", "fr")] * 2
        report = bias_summary(arts)
        assert report.most_overrepresented == ("en", "us")
        assert report.most_overrepresented_fraction == pytest.approx(0.8, abs=1e-6)

    def test_n_articles_and_strata_correct(self):
        arts = [_art("en", "us")] * 4 + [_art("fr", "fr")] * 3 + [_art("de", "de")] * 3
        report = bias_summary(arts)
        assert report.n_articles == 10
        assert report.n_strata == 3

    def test_strata_sorted_by_fraction_descending(self):
        arts = [_art("fr", "fr")] * 1 + [_art("en", "us")] * 6 + [_art("de", "de")] * 3
        report = bias_summary(arts)
        fracs = [s.fraction for s in report.strata]
        assert fracs == sorted(fracs, reverse=True)

    def test_entropy_corrected_greater_than_raw_for_skewed_input(self):
        """Correction should push entropy toward max (log2(n_strata))."""
        arts = _make_skewed_corpus(80, 20)  # heavily skewed
        report = bias_summary(arts)
        assert report.entropy_corrected > report.entropy_raw

    def test_entropy_raw_near_max_for_balanced_input(self):
        """Balanced corpus: raw entropy ≈ log2(n_strata)."""
        arts = [_art("en", "us")] * 5 + [_art("fr", "fr")] * 5
        report = bias_summary(arts)
        max_entropy = math.log2(2)
        assert report.entropy_raw == pytest.approx(max_entropy, abs=1e-5)

    def test_ess_less_than_n_for_skewed_corpus(self):
        """Strong skew reduces effective diversity below the raw count."""
        arts = _make_skewed_corpus(80, 20)
        report = bias_summary(arts)
        assert report.effective_sample_size < report.n_articles

    def test_ess_equals_n_for_balanced_corpus(self):
        """Perfectly balanced → ESS = n_articles (each article counts equally)."""
        arts = [_art("en", "us")] * 5 + [_art("fr", "fr")] * 5
        report = bias_summary(arts)
        assert report.effective_sample_size == pytest.approx(report.n_articles, abs=1e-3)

    def test_stratum_info_fields(self):
        arts = [_art("en", "us")] * 8 + [_art("fr", "fr")] * 2
        report = bias_summary(arts)
        en_info = next(s for s in report.strata if s.language == "en")
        fr_info = next(s for s in report.strata if s.language == "fr")
        assert en_info.count == 8
        assert en_info.fraction == pytest.approx(0.8, abs=1e-6)
        # minority stratum correction_weight > 1 (under-represented → boosted)
        assert fr_info.correction_weight > 1.0
        # majority stratum correction_weight < 1 (over-represented → down-weighted)
        assert en_info.correction_weight < 1.0

    def test_correction_weights_in_stratum_info_sum_check(self):
        """correction_weight * count / n_total should equal 1/n_strata for each stratum."""
        arts = [_art("en", "us")] * 6 + [_art("fr", "fr")] * 2 + [_art("de", "de")] * 2
        report = bias_summary(arts)
        target = 1.0 / report.n_strata
        for s in report.strata:
            # target_fraction = correction_weight * observed_fraction
            obs_frac = s.count / report.n_articles
            assert abs(s.correction_weight * obs_frac - target) < 1e-5

    def test_return_type(self):
        arts = [_art("en", "us")] * 3
        report = bias_summary(arts)
        assert isinstance(report, BiasReport)
        assert isinstance(report.strata[0], StratumInfo)


# ---------------------------------------------------------------------------
# End-to-end: 80% US-English skew scenario (the spec example)
# ---------------------------------------------------------------------------

class TestEightyPercentUSEnglishSkew:
    """80% US-English articles → IPW corrects toward a balanced prior."""

    def setup_method(self):
        # 80 en/us, 20 split over 3 other strata  → n=100
        self.corpus = _make_skewed_corpus(80, 20)
        self.report = bias_summary(self.corpus)
        self.weights = correction_weights(self.corpus)

    def test_dominant_stratum_identified(self):
        assert self.report.most_overrepresented == ("en", "us")
        assert self.report.most_overrepresented_fraction >= 0.79

    def test_entropy_improves_after_correction(self):
        max_entropy = math.log2(self.report.n_strata)
        gap_before = max_entropy - self.report.entropy_raw
        gap_after = max_entropy - self.report.entropy_corrected
        # correction should cut the entropy gap substantially
        assert gap_after < gap_before * 0.1, (
            f"entropy gap should shrink by >90%: before={gap_before:.4f}, after={gap_after:.4f}"
        )

    def test_corrected_distribution_near_uniform(self):
        """After correction each stratum should carry approximately equal weight."""
        from collections import Counter
        corrected: Counter = Counter()
        for art, w in zip(self.corpus, self.weights):
            lang = art.get("language", "unknown").lower()
            country = art.get("country", "unknown").lower()
            corrected[(lang, country)] += w
        total_w = sum(corrected.values())
        n_strata = len(corrected)
        target = total_w / n_strata
        for (lang, country), wt in corrected.items():
            ratio = wt / target
            assert abs(ratio - 1.0) < 0.05, (
                f"stratum ({lang}, {country}) carries {ratio:.3f}× target after correction"
            )

    def test_ess_substantially_below_raw_n(self):
        """ESS < 100 for a heavily skewed corpus."""
        assert self.report.effective_sample_size < 100

    def test_ess_above_minority_count(self):
        """The correction gives the minority strata meaningful weight, so ESS > 20."""
        assert self.report.effective_sample_size > 20

    def test_weights_sum_to_n(self):
        assert abs(sum(self.weights) - 100) < 1e-9

    def test_en_us_articles_downweighted(self):
        en_ws = [w for art, w in zip(self.corpus, self.weights)
                 if art.get("language") == "en" and art.get("country") == "us"]
        assert all(w < 1.0 for w in en_ws)

    def test_minority_articles_upweighted(self):
        minority_ws = [w for art, w in zip(self.corpus, self.weights)
                       if not (art.get("language") == "en" and art.get("country") == "us")]
        assert all(w > 1.0 for w in minority_ws)

    def test_all_weights_positive(self):
        assert all(w > 0 for w in self.weights)

    def test_bias_report_strata_count(self):
        # 4 distinct strata: en/us, fr/fr, de/de, ar/eg
        assert self.report.n_strata == 4


# ---------------------------------------------------------------------------
# cap_per_stratum — the de-slanting SELECTION (#40, §6.5) the backfill driver applies
# ---------------------------------------------------------------------------

class TestCapPerStratum:
    def test_empty_and_noop_cap(self):
        assert cap_per_stratum([], cap=5) == []
        arts = [_art("en", "us")] * 3
        assert cap_per_stratum(arts, cap=0) == arts  # cap<=0 → no-op (copy)

    def test_caps_overrepresented_major_keeps_long_tail(self):
        # 8 en/us majors + 2 fr/fr + 1 ar/eg long-tail; cap at 3.
        arts = [_art("en", "us")] * 8 + [_art("fr", "fr")] * 2 + [_art("ar", "eg")]
        kept = cap_per_stratum(arts, cap=3)
        dist = strata_distribution(kept)
        assert dist[("en", "us")] == 3       # the major is capped down
        assert dist[("fr", "fr")] == 2       # long tail (≤cap) passes through untouched
        assert dist[("ar", "eg")] == 1

    def test_deterministic_keeps_input_order_first_n(self):
        arts = [
            _art("en", "us", id=0), _art("en", "us", id=1),
            _art("en", "us", id=2), _art("fr", "fr", id=3),
        ]
        kept = cap_per_stratum(arts, cap=2)
        assert [a["id"] for a in kept] == [0, 1, 3]  # first two en/us + the fr/fr

    def test_cap_reduces_dominant_share(self):
        arts = _make_skewed_corpus(80, 20)
        before = bias_summary(arts).most_overrepresented_fraction
        after = bias_summary(cap_per_stratum(arts, cap=5)).most_overrepresented_fraction
        assert after < before  # the English-language major no longer dominates the replay
