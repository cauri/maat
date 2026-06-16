"""Backfill prior + archive-bias correction (P3, §8 — truth-over-time pillar).

Historical / archive backfill over-represents certain strata: older material skews
English-language, Western / US-UK outlets, and well-digitised sources.  Treating that
corpus at face value would amplify some voices and mute others in the veracity and
reputation signals.

This module corrects for that **before** the backfilled history feeds the learning loop:

  MEASURE  — ``strata_distribution`` counts each (language, country) cell relative to
              the total, exposing which cells are over- or under-represented.

  WEIGHT   — ``correction_weights`` applies inverse-propensity-style reweighting: each
              article receives a weight proportional to 1 / P(article's stratum) so that
              every cell contributes equally in expectation.  The weights are normalised
              to sum to the number of articles, keeping downstream aggregates on the same
              scale as an unweighted run.

  REPORT   — ``bias_summary`` bundles both into a single, operator-readable struct that
              surfaces the most over-represented stratum, the entropy of the raw vs
              corrected distribution, and the effective sample size after reweighting
              (ESS = (Σ w)² / Σ w²), which quantifies how much real diversity the
              corrected corpus represents.

Design notes
  - Pure functions over plain dicts — no DB, no I/O, no LLM.  Pass data in, get
    results out.
  - A stratum is (language, country).  Both fields default to ``"unknown"`` when absent
    so every article is always assigned to *some* cell (never silently dropped).
  - Weights that would be zero (empty strata) never arise — the denominator is always
    ≥ 1 by construction.
  - Entropy and ESS are measured on the *weighted* distribution, giving a single-number
    read on how balanced the correction made things and how much of the original N
    survives as effective observations.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

Stratum = tuple[str, str]  # (language, country)  — both lower-cased


@dataclass(frozen=True)
class StratumInfo:
    """Raw and relative counts for one (language, country) cell."""

    language: str
    country: str
    count: int
    fraction: float          # count / total_articles
    correction_weight: float  # target_fraction / observed_fraction  (≥ 1 means under-represented)


@dataclass(frozen=True)
class BiasReport:
    """Operator-readable summary of archive-bias detection and correction.

    Fields
    ------
    n_articles : int
        Total articles examined.
    n_strata : int
        Number of distinct (language, country) cells observed.
    most_overrepresented : tuple[str, str]
        The (language, country) pair whose ``observed_fraction`` most exceeds
        the balanced target (``1 / n_strata``).
    most_overrepresented_fraction : float
        Its share of the raw corpus.
    entropy_raw : float
        Shannon entropy (bits) of the raw stratum distribution.  Max is
        ``log2(n_strata)``; lower = more skewed.
    entropy_corrected : float
        Shannon entropy of the IPW-corrected distribution (should be close to
        max, confirming the weights work).
    effective_sample_size : float
        ESS = (Σ w)² / Σ w² across all articles.  A fully balanced corpus
        would give ESS = n_articles; strong skew → ESS ≪ n_articles.
    strata : list[StratumInfo]
        Per-cell detail, sorted by descending fraction (most dominant first).
    """

    n_articles: int
    n_strata: int
    most_overrepresented: tuple[str, str]
    most_overrepresented_fraction: float
    entropy_raw: float
    entropy_corrected: float
    effective_sample_size: float
    strata: list[StratumInfo]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stratum(article: dict[str, Any]) -> Stratum:
    """Extract the (language, country) cell from an article dict, with fallbacks."""
    lang = (article.get("language") or "unknown").strip().lower()
    country = (article.get("country") or "unknown").strip().lower()
    return lang, country


def _shannon_entropy(probs: list[float]) -> float:
    """Shannon entropy in bits over a list of probabilities (zeros ignored)."""
    return -sum(p * math.log2(p) for p in probs if p > 0)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

def strata_distribution(articles: list[dict[str, Any]]) -> dict[Stratum, int]:
    """Count articles per (language, country) stratum.

    Parameters
    ----------
    articles :
        Each dict must contain at least ``language`` and ``country`` keys
        (missing/None values default to ``"unknown"``).  Any other keys
        (source, date, veracity scores, …) are ignored.

    Returns
    -------
    dict mapping each observed (language, country) pair to its article count.
    Empty input returns an empty dict.
    """
    counts: Counter[Stratum] = Counter()
    for art in articles:
        counts[_stratum(art)] += 1
    return dict(counts)


def correction_weights(articles: list[dict[str, Any]]) -> list[float]:
    """Compute per-article inverse-propensity weights toward a balanced prior.

    The balanced target assigns equal probability to each *observed* stratum
    (1 / n_strata).  Each article is weighted by::

        w_i = target_fraction / observed_fraction_of_stratum_i
            = (1 / n_strata) / (count_i / n_total)
            = n_total / (n_strata * count_i)

    The weights are then normalised so they sum to ``n_articles``, which
    preserves the absolute scale of downstream aggregates while correcting
    the relative contribution of each stratum.

    Parameters
    ----------
    articles :
        Same format as ``strata_distribution``.

    Returns
    -------
    A list of floats, one per input article, in the same order.  All weights
    are positive.  Returns an empty list for empty input.
    """
    if not articles:
        return []

    counts = strata_distribution(articles)
    n_total = len(articles)
    n_strata = len(counts)

    # raw IPW: target = 1/n_strata, observed = count/n_total  → w = n_total / (n_strata * count)
    raw_weights = [
        n_total / (n_strata * counts[_stratum(art)])
        for art in articles
    ]

    # Normalise so Σ w = n_total (scale-preserving)
    w_sum = sum(raw_weights)
    scale = n_total / w_sum
    return [w * scale for w in raw_weights]


def cap_per_stratum(articles: list[dict[str, Any]], *, cap: int) -> list[dict[str, Any]]:
    """De-slant an archive corpus by capping each (language, country) stratum at ``cap`` articles.

    The pipeline downstream counts articles/originators, not IPW weights, so the most faithful way
    to keep archive over-representation from amplifying the English-language majors (§6.5) is to
    sub-sample the over-represented cells at SELECTION time. This keeps input order (deterministic,
    reproducible): the first ``cap`` articles of each stratum survive; long-tail cells (≤ ``cap``)
    pass through untouched. ``cap <= 0`` is a no-op (returns a copy). Pure — pairs with
    ``correction_weights``/``bias_summary`` (measure the skew, then cap to it).
    """
    if cap <= 0:
        return list(articles)
    seen: Counter[Stratum] = Counter()
    out: list[dict[str, Any]] = []
    for art in articles:
        s = _stratum(art)
        if seen[s] < cap:
            seen[s] += 1
            out.append(art)
    return out


def bias_summary(articles: list[dict[str, Any]]) -> BiasReport:
    """Measure archive-bias and return a full operator-readable report.

    Parameters
    ----------
    articles :
        Each dict should contain ``language`` and ``country`` (both optional;
        missing → ``"unknown"``).  Any additional keys are ignored.

    Returns
    -------
    ``BiasReport`` — see its docstring for field semantics.

    Raises
    ------
    ValueError
        If ``articles`` is empty (no distribution to measure).
    """
    if not articles:
        raise ValueError("bias_summary requires at least one article")

    counts = strata_distribution(articles)
    weights = correction_weights(articles)
    n_total = len(articles)
    n_strata = len(counts)
    target_fraction = 1.0 / n_strata

    # Per-stratum info
    stratum_infos: list[StratumInfo] = []
    for (lang, country), cnt in counts.items():
        obs_fraction = cnt / n_total
        stratum_infos.append(StratumInfo(
            language=lang,
            country=country,
            count=cnt,
            fraction=round(obs_fraction, 6),
            correction_weight=round(target_fraction / obs_fraction, 6),
        ))
    stratum_infos.sort(key=lambda s: s.fraction, reverse=True)

    # Most over-represented = highest fraction (furthest above the flat target)
    top = stratum_infos[0]

    # Raw entropy
    raw_probs = [s.fraction for s in stratum_infos]
    entropy_raw = round(_shannon_entropy(raw_probs), 6)

    # Corrected entropy: weight each article, then aggregate per stratum
    corrected_counts: Counter[Stratum] = Counter()
    for art, w in zip(articles, weights):
        corrected_counts[_stratum(art)] += w
    total_w = sum(corrected_counts.values())
    corrected_probs = [v / total_w for v in corrected_counts.values()]
    entropy_corrected = round(_shannon_entropy(corrected_probs), 6)

    # Effective sample size  ESS = (Σw)² / Σw²
    w_sq_sum = sum(w ** 2 for w in weights)
    ess = round((sum(weights) ** 2) / w_sq_sum, 4) if w_sq_sum > 0 else float(n_total)

    return BiasReport(
        n_articles=n_total,
        n_strata=n_strata,
        most_overrepresented=(top.language, top.country),
        most_overrepresented_fraction=top.fraction,
        entropy_raw=entropy_raw,
        entropy_corrected=entropy_corrected,
        effective_sample_size=ess,
        strata=stratum_infos,
    )
