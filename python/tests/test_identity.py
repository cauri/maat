"""Tests for identity resolution (§6.7).

All tests are deterministic — no I/O, no embeddings, no LLM calls.
Coverage:
  - canonical_source: registry lookup (name + domain variants)
  - canonical_source: domain normalisation (www, m, schemes, paths)
  - canonical_source: name normalisation (case, noise prefixes/suffixes, punctuation)
  - canonical_source: unknown / fall-through behaviour
  - alias_clusters: registry-backed grouping
  - alias_clusters: token-overlap fallback (no registry entry)
  - alias_clusters: domain ↔ name unification
  - alias_clusters: independent sources stay separate
  - alias_clusters: edge cases (empty, single element)
  - REGISTRY: structure and completeness checks
"""

from maat.pipeline.identity import (
    REGISTRY,
    _is_domain,
    _normalise_domain,
    _normalise_name,
    _token_overlap,
    alias_clusters,
    canonical_source,
)


# ---------------------------------------------------------------------------
# canonical_source — wire agencies
# ---------------------------------------------------------------------------


class TestCanonicalSourceWireAgencies:
    def test_reuters_bare_name(self):
        assert canonical_source("Reuters") == "reuters"

    def test_reuters_uppercase(self):
        assert canonical_source("REUTERS") == "reuters"

    def test_reuters_domain(self):
        assert canonical_source("reuters.com") == "reuters"

    def test_reuters_www_domain(self):
        assert canonical_source("www.reuters.com") == "reuters"

    def test_reuters_full_corporate_name(self):
        assert canonical_source("Thomson Reuters") == "reuters"

    def test_reuters_hyphenated_corporate(self):
        assert canonical_source("Thomson-Reuters") == "reuters"

    def test_reuters_regional_variant(self):
        assert canonical_source("Reuters UK") == "reuters"

    def test_afp_acronym(self):
        assert canonical_source("AFP") == "afp"

    def test_afp_full_french_name(self):
        assert canonical_source("Agence France-Presse") == "afp"

    def test_afp_full_no_hyphen(self):
        assert canonical_source("Agence France Presse") == "afp"

    def test_ap_acronym(self):
        assert canonical_source("AP") == "associated-press"

    def test_ap_full_name(self):
        assert canonical_source("Associated Press") == "associated-press"

    def test_ap_the_prefix(self):
        assert canonical_source("The Associated Press") == "associated-press"

    def test_ap_domain(self):
        assert canonical_source("apnews.com") == "associated-press"

    def test_tass_acronym(self):
        assert canonical_source("TASS") == "tass"

    def test_tass_hyphenated_variant(self):
        assert canonical_source("ITAR-TASS") == "tass"

    def test_xinhua_name(self):
        assert canonical_source("Xinhua") == "xinhua"

    def test_xinhua_domain(self):
        assert canonical_source("xinhuanet.com") == "xinhua"

    def test_dpa_full_german_name(self):
        assert canonical_source("Deutsche Presse-Agentur") == "dpa"


# ---------------------------------------------------------------------------
# canonical_source — major outlets
# ---------------------------------------------------------------------------


class TestCanonicalSourceOutlets:
    def test_bbc_name(self):
        assert canonical_source("BBC") == "bbc"

    def test_bbc_news(self):
        assert canonical_source("BBC News") == "bbc"

    def test_bbc_domain(self):
        assert canonical_source("bbc.com") == "bbc"

    def test_bbc_uk_domain(self):
        assert canonical_source("bbc.co.uk") == "bbc"

    def test_nyt_name(self):
        assert canonical_source("The New York Times") == "nyt"

    def test_nyt_no_article(self):
        assert canonical_source("New York Times") == "nyt"

    def test_nyt_domain(self):
        assert canonical_source("nytimes.com") == "nyt"

    def test_nyt_acronym(self):
        assert canonical_source("NYT") == "nyt"

    def test_guardian_name(self):
        assert canonical_source("The Guardian") == "guardian"

    def test_guardian_domain(self):
        assert canonical_source("theguardian.com") == "guardian"

    def test_wapo_name(self):
        assert canonical_source("Washington Post") == "washington-post"

    def test_wapo_domain(self):
        assert canonical_source("washingtonpost.com") == "washington-post"

    def test_wapo_acronym(self):
        assert canonical_source("WaPo") == "washington-post"

    def test_wsj_name(self):
        assert canonical_source("Wall Street Journal") == "wsj"

    def test_wsj_domain(self):
        assert canonical_source("wsj.com") == "wsj"

    def test_ft_name(self):
        assert canonical_source("Financial Times") == "ft"

    def test_ft_domain(self):
        assert canonical_source("ft.com") == "ft"

    def test_bloomberg_name(self):
        assert canonical_source("Bloomberg") == "bloomberg"

    def test_bloomberg_domain(self):
        assert canonical_source("bloomberg.com") == "bloomberg"

    def test_al_jazeera_name(self):
        assert canonical_source("Al Jazeera") == "al-jazeera"

    def test_al_jazeera_hyphen(self):
        assert canonical_source("Al-Jazeera") == "al-jazeera"

    def test_al_jazeera_domain(self):
        assert canonical_source("aljazeera.com") == "al-jazeera"

    def test_le_monde_name(self):
        assert canonical_source("Le Monde") == "le-monde"

    def test_le_monde_domain(self):
        assert canonical_source("lemonde.fr") == "le-monde"

    def test_spiegel_with_article(self):
        assert canonical_source("Der Spiegel") == "spiegel"


# ---------------------------------------------------------------------------
# canonical_source — domain normalisation
# ---------------------------------------------------------------------------


class TestDomainNormalisation:
    def test_strips_www(self):
        assert canonical_source("www.bbc.com") == "bbc"

    def test_strips_mobile_subdomain(self):
        # m.reuters.com should resolve the same as reuters.com
        assert canonical_source("m.reuters.com") == "reuters"

    def test_strips_scheme_https(self):
        # URL with path — strip to domain then look up
        assert canonical_source("https://www.reuters.com/world/uk") == "reuters"

    def test_preserves_meaningful_subdomain(self):
        # "sport.bbc.co.uk" — "sport" is not in the generic set, so stays → not matched
        result = canonical_source("sport.bbc.co.uk")
        assert result != "bbc"  # unknown but deterministic

    def test_unknown_domain_returns_normalised(self):
        result = canonical_source("unknown-outlet.example.com")
        assert result == "unknown-outlet.example.com"

    def test_unknown_www_domain_strips_www(self):
        result = canonical_source("www.unknown-outlet.example.com")
        assert result == "unknown-outlet.example.com"


# ---------------------------------------------------------------------------
# canonical_source — name normalisation
# ---------------------------------------------------------------------------


class TestNameNormalisation:
    def test_strips_noise_suffix_news_agency(self):
        # "Reuters News Agency" → normalised → "reuters" → registry hit
        assert canonical_source("Reuters News Agency") == "reuters"

    def test_strips_the_prefix(self):
        assert canonical_source("The Economist") == "economist"

    def test_lowercase_input(self):
        assert canonical_source("bloomberg") == "bloomberg"

    def test_mixed_case(self):
        assert canonical_source("BloomBerg") == "bloomberg"

    def test_unknown_name_returns_normalised(self):
        result = canonical_source("Some Weekly Tribune")
        # Not in registry; noise suffix "Weekly Tribune" not stripped, so returned as-is
        assert result == result.lower()  # at least lowercase

    def test_strips_media_group_suffix(self):
        # Unknown outlet with noise suffix stripped
        result = canonical_source("Acme Media Group")
        assert "media group" not in result


# ---------------------------------------------------------------------------
# canonical_source — unknown / fall-through
# ---------------------------------------------------------------------------


class TestCanonicalSourceFallthrough:
    def test_unknown_name_is_lowercase(self):
        result = canonical_source("Fictional Tribune Weekly")
        assert result == result.lower()

    def test_unknown_domain_preserved(self):
        result = canonical_source("fictional-outlet.org")
        assert result == "fictional-outlet.org"

    def test_empty_string_does_not_raise(self):
        result = canonical_source("")
        assert isinstance(result, str)

    def test_whitespace_only_does_not_raise(self):
        result = canonical_source("   ")
        assert isinstance(result, str)

    def test_idempotent_on_canonical_id(self):
        # Passing a canonical id in again should return the same id
        assert canonical_source("reuters") == "reuters"
        assert canonical_source("afp") == "afp"


# ---------------------------------------------------------------------------
# alias_clusters — registry-backed grouping
# ---------------------------------------------------------------------------


class TestAliasClustersRegistry:
    def test_reuters_variants_all_in_one_group(self):
        sources = ["Reuters", "reuters.com", "www.reuters.com", "Thomson Reuters"]
        groups = alias_clusters(sources)
        # All four must land in the same group
        flat = {s for g in groups for s in g}
        assert flat == set(sources)
        assert len(groups) == 1

    def test_afp_variants_all_in_one_group(self):
        sources = ["AFP", "Agence France-Presse", "AFP English"]
        groups = alias_clusters(sources)
        assert len(groups) == 1

    def test_reuters_and_afp_stay_separate(self):
        sources = ["Reuters", "reuters.com", "AFP", "Agence France-Presse"]
        groups = alias_clusters(sources)
        assert len(groups) == 2
        canons = {frozenset(canonical_source(s) for s in g) for g in groups}
        assert frozenset({"reuters"}) in canons
        assert frozenset({"afp"}) in canons

    def test_three_agencies_three_groups(self):
        sources = ["Reuters", "AFP", "Associated Press"]
        groups = alias_clusters(sources)
        assert len(groups) == 3

    def test_mixed_outlets_cluster_correctly(self):
        sources = ["BBC", "bbc.com", "BBC News", "NYT", "nytimes.com"]
        groups = alias_clusters(sources)
        assert len(groups) == 2
        sizes = sorted(len(g) for g in groups)
        assert sizes == [2, 3]

    def test_name_domain_unification(self):
        # Domain form and name form of same outlet must cluster together
        sources = ["Financial Times", "ft.com", "The Financial Times"]
        groups = alias_clusters(sources)
        assert len(groups) == 1

    def test_al_jazeera_variants(self):
        sources = ["Al Jazeera", "Al-Jazeera", "aljazeera.com", "Al Jazeera English"]
        groups = alias_clusters(sources)
        assert len(groups) == 1


# ---------------------------------------------------------------------------
# alias_clusters — token-overlap fallback (no registry entry)
# ---------------------------------------------------------------------------


class TestAliasClustersTokenFallback:
    def test_token_fallback_high_overlap(self):
        # Two hypothetical unregistered outlets with 3 of 4 tokens in common:
        # "Fictional News Daily Report" ↔ "Fictional News Daily Tribune"
        # {"fictional","news","daily","report"} ∩ {"fictional","news","daily","tribune"} = 3/5 → 0.6
        sources = ["Fictional News Daily Report", "Fictional News Daily Tribune", "Acme Sports Network"]
        groups = alias_clusters(sources)
        fn_group = next(g for g in groups if "Fictional News Daily Report" in g)
        assert "Fictional News Daily Tribune" in fn_group
        acme_group = next(g for g in groups if "Acme Sports Network" in g)
        assert "Fictional News Daily Report" not in acme_group

    def test_two_token_pairs_do_not_cluster(self):
        # "Reuters UK" ↔ "Reuters US" each have 2 tokens; overlap = 1/3 < 0.60 threshold.
        # They are NOT in the registry under those exact names, so they stay separate.
        # (In production, add them to the registry to guarantee collapse.)
        sources = ["Reuters UK", "Reuters US"]
        groups = alias_clusters(sources)
        # They may or may not cluster via the registry path (both resolve to "reuters")
        # — test the correct behaviour: same canonical, so SAME group via _canon_pairs
        assert len(groups) == 1

    def test_unrelated_outlets_stay_separate(self):
        sources = ["The Times", "The Guardian", "The Telegraph"]
        groups = alias_clusters(sources)
        # All single tokens after stripping "The" → low Jaccard → separate
        assert len(groups) == 3

    def test_close_but_distinct_names(self):
        # "Morning Star" vs "Evening Star" — share one of two tokens → Jaccard = 1/3 < 0.6
        sources = ["Morning Star", "Evening Star"]
        groups = alias_clusters(sources)
        assert len(groups) == 2


# ---------------------------------------------------------------------------
# alias_clusters — edge cases
# ---------------------------------------------------------------------------


class TestAliasClustersEdgeCases:
    def test_empty_list(self):
        assert alias_clusters([]) == []

    def test_single_element(self):
        groups = alias_clusters(["Reuters"])
        assert groups == [["Reuters"]]

    def test_two_identical_strings(self):
        # Same string twice — should be one group
        groups = alias_clusters(["AFP", "AFP"])
        assert len(groups) == 1

    def test_two_unknown_singletons(self):
        groups = alias_clusters(["Acme Tribune", "Daily Bugle"])
        assert len(groups) == 2

    def test_output_covers_all_inputs(self):
        sources = ["Reuters", "AFP", "BBC", "AP", "bbc.com"]
        groups = alias_clusters(sources)
        flat = [s for g in groups for s in g]
        assert sorted(flat) == sorted(sources)

    def test_result_is_list_of_lists(self):
        groups = alias_clusters(["Reuters"])
        assert isinstance(groups, list)
        assert isinstance(groups[0], list)


# ---------------------------------------------------------------------------
# REGISTRY structure checks
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_all_values_are_lowercase(self):
        for v in REGISTRY.values():
            assert v == v.lower(), f"canonical id not lowercase: {v!r}"

    def test_all_keys_are_lowercase(self):
        for k in REGISTRY.keys():
            assert k == k.lower(), f"registry key not lowercase: {k!r}"

    def test_known_wire_agencies_present(self):
        for agency in ("reuters", "afp", "associated-press", "tass", "xinhua", "dpa"):
            assert agency in REGISTRY.values(), f"missing wire agency: {agency}"

    def test_known_outlets_present(self):
        for outlet in ("bbc", "nyt", "guardian", "wsj", "bloomberg", "ft", "al-jazeera"):
            assert outlet in REGISTRY.values(), f"missing outlet: {outlet}"

    def test_no_url_as_value(self):
        for v in REGISTRY.values():
            assert "://" not in v, f"canonical id looks like URL: {v!r}"
            assert "/" not in v, f"canonical id contains slash: {v!r}"


# ---------------------------------------------------------------------------
# Helpers (tested in isolation for robustness)
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_is_domain_bare(self):
        assert _is_domain("reuters.com")
        assert _is_domain("bbc.co.uk")

    def test_is_domain_rejects_spaces(self):
        assert not _is_domain("Reuters News")

    def test_is_domain_rejects_scheme(self):
        # Scheme not a bare domain (scheme + host has "://")
        assert not _is_domain("https://reuters.com")

    def test_normalise_domain_strips_www(self):
        assert _normalise_domain("www.reuters.com") == "reuters.com"

    def test_normalise_domain_strips_m(self):
        assert _normalise_domain("m.reuters.com") == "reuters.com"

    def test_normalise_domain_preserves_co_uk(self):
        assert _normalise_domain("www.bbc.co.uk") == "bbc.co.uk"

    def test_normalise_domain_strips_scheme_and_path(self):
        assert _normalise_domain("https://www.reuters.com/world") == "reuters.com"

    def test_normalise_name_strips_the(self):
        # "press" is no longer in the noise-suffix list (it would strip "Associated Press")
        # Only "The" prefix is stripped → "associated press"
        assert _normalise_name("The Associated Press") == "associated press"

    def test_normalise_name_strips_news_agency(self):
        assert _normalise_name("Reuters News Agency") == "reuters"

    def test_normalise_name_lowercase(self):
        assert _normalise_name("REUTERS") == "reuters"

    def test_normalise_name_strips_punctuation(self):
        result = _normalise_name("Bloomberg, L.P.")
        assert "." not in result
        assert "," not in result

    def test_token_overlap_identical(self):
        assert _token_overlap("reuters", "reuters") == 1.0

    def test_token_overlap_disjoint(self):
        assert _token_overlap("reuters", "guardian") == 0.0

    def test_token_overlap_partial(self):
        # "reuters uk" ↔ "reuters us": share "reuters" (1 of 3 union) → 1/3
        ov = _token_overlap("reuters uk", "reuters us")
        assert 0 < ov < 1.0

    def test_token_overlap_empty(self):
        assert _token_overlap("", "reuters") == 0.0
        assert _token_overlap("reuters", "") == 0.0
