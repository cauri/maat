"""De-US-centering metrics (BRIEF §8, P7).

Measure whether the Maat feed actually counters Anglo-American slant. Given a set of stories
or sources with geographic and language metadata, this module computes:

  - geographic_distribution  — fraction of sources per country/region
  - language_distribution    — fraction of content per language
  - anglo_share              — combined US + UK share of sources (the key slant signal)
  - herfindahl               — single-country concentration (HHI, 0–1); 0 = perfectly spread,
                               1 = one country owns everything
  - originator_country_count — how many distinct originator countries appear in the feed
  - score                    — de-US-centering composite score (0–1); 1 = maximally diverse

All functions are PURE and DETERMINISTIC — no I/O, no DB, testable without infrastructure.
The ``score`` function accepts configurable targets via ``Targets``; defaults are calibrated
for the mission (counter Anglo-American dominance, amplify Global South voices).

This is the measurement layer consumed by P7 validation and the P8 dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceMeta:
    """Metadata for one story/article in the feed.

    ``source_country`` is the two-letter ISO 3166-1 alpha-2 code for the country where the
    outlet originates (not where the news happened). ``language`` is a BCP-47 tag or a simple
    ISO 639-1 code (e.g. ``"en"``, ``"ar"``, ``"pt"``).  Both may be ``None`` if unknown —
    unknown entries are excluded from the denominator of the relevant distribution but still
    count against overall completeness.
    """

    source_country: str | None  # ISO 3166-1 alpha-2, e.g. "US", "CN", "NG"
    language: str | None        # ISO 639-1 / BCP-47, e.g. "en", "ar", "pt-BR"


# Countries that constitute the "Anglo-American bloc" for slant measurement.
# US and UK by mission definition; expand with care and rationale.
ANGLO_COUNTRIES: frozenset[str] = frozenset({"US", "GB"})


# ---------------------------------------------------------------------------
# Targets — configurable thresholds for the composite score
# ---------------------------------------------------------------------------


@dataclass
class Targets:
    """Target thresholds for each axis; score = 1.0 when all targets are met or exceeded.

    Defaults calibrated to Maat's mission:
      - no_anglo_above:        Anglo share should stay BELOW 40 % (below => full credit)
      - hhi_below:             HHI should stay BELOW 0.25 (low concentration)
      - min_countries:         at least 10 distinct originator countries
      - min_languages:         at least 5 distinct languages
      - no_single_lang_above:  no single language above 60 % (prevents English monoculture)
    """

    no_anglo_above: float = 0.40        # Anglo share ceiling (fraction, 0–1)
    hhi_below: float = 0.25             # HHI ceiling (0–1); below = good
    min_countries: int = 10             # minimum distinct originator countries
    min_languages: int = 5              # minimum distinct languages
    no_single_lang_above: float = 0.60  # single-language share ceiling


# ---------------------------------------------------------------------------
# Per-axis metric functions
# ---------------------------------------------------------------------------


def geographic_distribution(sources: list[SourceMeta]) -> dict[str, float]:
    """Fraction of sources by country code (known countries only).

    Returns an empty dict for an empty feed or when no country data is available.
    Unknown countries (``None``) are excluded from the denominator.

    >>> geographic_distribution([SourceMeta("US", "en"), SourceMeta("NG", "ha"), SourceMeta("US", "en")])
    {'US': 0.6667, 'NG': 0.3333}
    """
    counts: dict[str, int] = {}
    for s in sources:
        if s.source_country is not None:
            counts[s.source_country] = counts.get(s.source_country, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return {}
    return {k: round(v / total, 4) for k, v in sorted(counts.items(), key=lambda x: -x[1])}


def language_distribution(sources: list[SourceMeta]) -> dict[str, float]:
    """Fraction of sources by language tag (known languages only).

    Language tags are normalised to lowercase.  Unknown languages (``None``) are excluded.

    >>> language_distribution([SourceMeta("US", "en"), SourceMeta("NG", "ha"), SourceMeta("NG", "en")])
    {'en': 0.6667, 'ha': 0.3333}
    """
    counts: dict[str, int] = {}
    for s in sources:
        if s.language is not None:
            lang = s.language.lower()
            counts[lang] = counts.get(lang, 0) + 1
    total = sum(counts.values())
    if total == 0:
        return {}
    return {k: round(v / total, 4) for k, v in sorted(counts.items(), key=lambda x: -x[1])}


def anglo_share(sources: list[SourceMeta]) -> float:
    """Fraction of sources from Anglo-American countries (US or GB), by country metadata.

    Sources with unknown country are excluded from the denominator.
    Returns 0.0 for an empty feed or when no country data is present.

    >>> anglo_share([SourceMeta("US", "en"), SourceMeta("GB", "en"), SourceMeta("NG", "ha")])
    0.6667
    """
    known = [s for s in sources if s.source_country is not None]
    if not known:
        return 0.0
    anglo = sum(1 for s in known if s.source_country in ANGLO_COUNTRIES)
    return round(anglo / len(known), 4)


def herfindahl(sources: list[SourceMeta]) -> float:
    """Herfindahl-Hirschman Index for single-country concentration (0–1).

    HHI = sum of squared shares per country.  0 = perfectly diversified (infinitely many
    equal-share countries); 1 = monopoly (one country provides all sources).

    Sources with unknown country are excluded from the computation.
    Returns 1.0 for a single source (or all from one country), 0.0 for no known sources.

    >>> herfindahl([SourceMeta("US", "en"), SourceMeta("US", "en")])
    1.0
    >>> herfindahl([SourceMeta("US", "en"), SourceMeta("NG", "ha")])
    0.5
    """
    dist = geographic_distribution(sources)
    if not dist:
        return 0.0
    return round(sum(v ** 2 for v in dist.values()), 4)


def originator_country_count(sources: list[SourceMeta]) -> int:
    """Number of distinct originator countries in the feed (known countries only).

    >>> originator_country_count([SourceMeta("US", "en"), SourceMeta("NG", "ha"), SourceMeta("US", "es")])
    2
    """
    return len({s.source_country for s in sources if s.source_country is not None})


# ---------------------------------------------------------------------------
# Composite score
# ---------------------------------------------------------------------------


class ScoreBreakdown(NamedTuple):
    """Per-axis scores (each 0–1) and the composite.

    Each axis contributes equally to ``overall``.  A value of 1.0 means the axis fully meets
    its target; 0.0 means it misses entirely.  Partial credit is linear between 0 and target.
    """

    anglo: float          # 1.0 when Anglo share ≤ target
    concentration: float  # 1.0 when HHI ≤ target
    country_diversity: float  # 1.0 when distinct countries ≥ target
    language_diversity: float  # 1.0 when distinct languages ≥ target
    language_dominance: float  # 1.0 when no single language exceeds ceiling
    overall: float        # mean of the five axes


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, v))


def score(sources: list[SourceMeta], targets: Targets | None = None) -> ScoreBreakdown:
    """Compute the de-US-centering score (0–1) with a per-axis breakdown.

    0 = feed is entirely US/UK-dominated and monolingual.
    1 = feed fully meets all diversity targets.

    Each axis is scored independently (partial credit is linear); the overall score is the
    unweighted mean of the five axes.  Pass ``targets`` to override the defaults.

    Edge cases:
      - Empty feed → all zeros (no information → no diversity credit).
      - Single source → scores entirely determined by that source's metadata.
      - All sources unknown country → country axes score 0.

    >>> s = score([])
    >>> s.overall
    0.0
    """
    if targets is None:
        targets = Targets()

    if not sources:
        return ScoreBreakdown(
            anglo=0.0,
            concentration=0.0,
            country_diversity=0.0,
            language_diversity=0.0,
            language_dominance=0.0,
            overall=0.0,
        )

    # Axis 1: Anglo share — lower is better.
    # Full credit when share ≤ target ceiling.  Partial credit as share rises toward ceiling.
    # Above the ceiling: credit falls from 1.0 at target down to 0.0 at 100% Anglo.
    a_share = anglo_share(sources)
    ceil = targets.no_anglo_above
    if a_share <= ceil:
        anglo_score = 1.0
    else:
        # linearly degrades: 1.0 at ceil, 0.0 at 1.0
        anglo_score = _clamp(1.0 - (a_share - ceil) / (1.0 - ceil)) if ceil < 1.0 else 0.0

    # Axis 2: HHI concentration — lower is better.
    hhi = herfindahl(sources)
    hhi_ceil = targets.hhi_below
    if hhi <= hhi_ceil:
        concentration_score = 1.0
    else:
        concentration_score = _clamp(1.0 - (hhi - hhi_ceil) / (1.0 - hhi_ceil)) if hhi_ceil < 1.0 else 0.0

    # Axis 3: country diversity — more distinct countries is better.
    n_countries = originator_country_count(sources)
    target_countries = targets.min_countries
    country_div_score = _clamp(n_countries / target_countries) if target_countries > 0 else 1.0

    # Axis 4: language diversity — more distinct languages is better.
    lang_dist = language_distribution(sources)
    n_langs = len(lang_dist)
    target_langs = targets.min_languages
    lang_div_score = _clamp(n_langs / target_langs) if target_langs > 0 else 1.0

    # Axis 5: language dominance — no single language should dominate.
    if lang_dist:
        top_lang_share = max(lang_dist.values())
        lang_ceil = targets.no_single_lang_above
        if top_lang_share <= lang_ceil:
            lang_dom_score = 1.0
        else:
            lang_dom_score = _clamp(1.0 - (top_lang_share - lang_ceil) / (1.0 - lang_ceil)) if lang_ceil < 1.0 else 0.0
    else:
        lang_dom_score = 0.0  # no language data → no credit

    overall = round((anglo_score + concentration_score + country_div_score + lang_div_score + lang_dom_score) / 5, 4)

    return ScoreBreakdown(
        anglo=round(anglo_score, 4),
        concentration=round(concentration_score, 4),
        country_diversity=round(country_div_score, 4),
        language_diversity=round(lang_div_score, 4),
        language_dominance=round(lang_dom_score, 4),
        overall=overall,
    )
