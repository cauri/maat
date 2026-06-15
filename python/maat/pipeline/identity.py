"""Identity resolution for news sources / originators (BRIEF §6.7).

`corroborate.is_primary_source` and `collapse_originators` use raw source strings — a name
or domain typed by the ingestion agent. The same wire service may appear as "Reuters",
"reuters.com", "www.reuters.com", "Thomson Reuters", or "REUTERS". Without resolution those
four strings count as four separate originators, distorting the corroboration read.

This module provides:
  - `canonical_source(name_or_domain) -> str`  — map ANY variant to ONE canonical id
  - `alias_clusters(sources) -> list[list[str]]` — group a list of source strings into
    equivalence classes of aliases (without a registry: pure statistical / structural signals)
  - `REGISTRY`                                  — seed map of known alias → canonical id

Design constraints (Gamelan discipline):
  - Pure functions, deterministic, no I/O, no RNG.
  - Derive from structure: domain normalisation → name normalisation → registry lookup.
  - `corroborate.py` can call `canonical_source` at the point it builds `art_source`;
    no other caller changes needed.

DRAFT — review thresholds and registry completeness with cauri.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Seed registry: known alias → canonical id
#
# The canonical id is a short, stable, human-readable string — never a URL, never
# localised.  Keep it lowercase so comparisons are case-insensitive by construction.
# ---------------------------------------------------------------------------

#: Map of raw alias (lowercased) → canonical id.
#: All variants that should resolve to the same originator are listed here.
_RAW_REGISTRY: dict[str, str] = {
    # ----- Wire agencies -----
    "reuters": "reuters",
    "reuters.com": "reuters",
    "www.reuters.com": "reuters",
    "thomson reuters": "reuters",
    "thomson-reuters": "reuters",
    "reuters news agency": "reuters",
    "reuters uk": "reuters",
    "reuters us": "reuters",

    "afp": "afp",
    "agence france-presse": "afp",
    "agence france presse": "afp",
    "afp news": "afp",
    "afp english": "afp",

    "ap": "associated-press",
    "associated press": "associated-press",
    "the associated press": "associated-press",
    "apnews.com": "associated-press",
    "ap news": "associated-press",
    "ap newswire": "associated-press",

    "upa": "united-press-international",
    "upi": "united-press-international",
    "united press international": "united-press-international",

    "dpa": "dpa",
    "dpa international": "dpa",
    "dpa-international": "dpa",
    "deutsche presse-agentur": "dpa",

    "efe": "efe",
    "agencia efe": "efe",
    "efe news agency": "efe",

    "itar-tass": "tass",
    "tass": "tass",
    "tass.com": "tass",
    "tass russian news agency": "tass",

    "xinhua": "xinhua",
    "xinhuanet.com": "xinhua",
    "xinhua news agency": "xinhua",
    "新华社": "xinhua",

    # ----- Major outlets -----
    "bbc": "bbc",
    "bbc news": "bbc",
    "bbc.com": "bbc",
    "bbc.co.uk": "bbc",
    "bbc world service": "bbc",

    "cnn": "cnn",
    "cnn.com": "cnn",
    "cnn international": "cnn",
    "cnn breaking news": "cnn",

    "the new york times": "nyt",
    "new york times": "nyt",
    "nytimes.com": "nyt",
    "nyt": "nyt",

    "the guardian": "guardian",
    "guardian": "guardian",
    "theguardian.com": "guardian",
    "guardian us": "guardian",
    "guardian uk": "guardian",

    "the washington post": "washington-post",
    "washington post": "washington-post",
    "washingtonpost.com": "washington-post",
    "wapo": "washington-post",
    "wp": "washington-post",

    "the wall street journal": "wsj",
    "wall street journal": "wsj",
    "wsj": "wsj",
    "wsj.com": "wsj",

    "al jazeera": "al-jazeera",
    "aljazeera": "al-jazeera",
    "aljazeera.com": "al-jazeera",
    "al jazeera english": "al-jazeera",
    "al-jazeera": "al-jazeera",

    "financial times": "ft",
    "the financial times": "ft",
    "ft": "ft",
    "ft.com": "ft",

    "bloomberg": "bloomberg",
    "bloomberg.com": "bloomberg",
    "bloomberg news": "bloomberg",
    "bloomberg l.p.": "bloomberg",

    "the economist": "economist",
    "economist.com": "economist",
    "economist": "economist",

    "le monde": "le-monde",
    "lemonde.fr": "le-monde",

    "der spiegel": "spiegel",
    "spiegel": "spiegel",
    "spiegel.de": "spiegel",

    "politico": "politico",
    "politico.com": "politico",
    "politico eu": "politico",
    "politico europe": "politico",
}

#: Canonical registry with normalised keys (built once at import time).
REGISTRY: dict[str, str] = {k.strip().lower(): v for k, v in _RAW_REGISTRY.items()}


# ---------------------------------------------------------------------------
# Domain normalisation helpers
# ---------------------------------------------------------------------------

_STRIP_SCHEME = re.compile(r"^https?://", re.IGNORECASE)
_STRIP_PATH = re.compile(r"/.*$")
_GENERIC_SUBDOMAINS = frozenset({"www", "m", "mobile", "amp", "news", "en", "int"})
_HAS_SCHEME = re.compile(r"^https?://", re.IGNORECASE)


def _normalise_domain(raw: str) -> str:
    """Strip scheme, path, query, and generic subdomains from a domain-like string.

    "https://www.reuters.com/world" → "reuters.com"
    "m.theguardian.com"            → "theguardian.com"
    """
    s = _STRIP_SCHEME.sub("", raw.strip())
    s = _STRIP_PATH.sub("", s)
    parts = s.lower().split(".")
    # Drop leading generic subdomains (www, m, amp, …) but never the TLD pair.
    while len(parts) > 2 and parts[0] in _GENERIC_SUBDOMAINS:
        parts = parts[1:]
    return ".".join(parts)


_LOOKS_LIKE_DOMAIN = re.compile(
    r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$", re.IGNORECASE
)


def _is_domain(s: str) -> bool:
    """Return True if `s` looks like a bare domain (no scheme, no spaces)."""
    return bool(_LOOKS_LIKE_DOMAIN.match(s.strip()))


# ---------------------------------------------------------------------------
# Name normalisation helpers
# ---------------------------------------------------------------------------

_NOISE_PREFIXES = re.compile(
    r"^(the|le|la|les|el|los|las|der|die|das|de|het)\s+", re.IGNORECASE
)
_NOISE_SUFFIXES = re.compile(
    # Only strip suffixes that follow at least one non-suffix word — "news agency" etc.
    # "press" alone is NOT stripped (e.g. "Associated Press" keeps both tokens so the
    # registry can match "associated press").  Multi-word compound suffixes come first.
    r"(?<=\S)\s+(news\s+agency|news\s+service|media\s+group|media|group|network|online|digital|wire|international|agency)$",
    re.IGNORECASE,
)
# Punctuation and extra whitespace
_PUNCT = re.compile(r"[.,'\"!?;:()\[\]{}<>]")
_WS = re.compile(r"\s+")


def _normalise_name(raw: str) -> str:
    """Reduce a human-typed outlet name to a comparable token string.

    "The Associated Press"  → "associated press"
    "Reuters News Agency"   → "reuters"
    "REUTERS"               → "reuters"
    """
    s = raw.strip().lower()
    s = _NOISE_PREFIXES.sub("", s)
    s = _NOISE_SUFFIXES.sub("", s)
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def canonical_source(name_or_domain: str) -> str:
    """Map any source variant to a canonical originator id.

    Resolution order:
    1. Direct registry lookup (lowercased raw input).
    2. If the input looks like a domain, normalise it (strip www/m/…) and look up.
    3. Normalise as a name (strip noise prefixes/suffixes, lowercase) and look up.
    4. Fall back to the normalised name itself — an unknown source is its own canonical form.

    The returned id is always lowercase.  Callers in ``corroborate.py`` can replace raw
    ``source`` strings with the canonical form before building ``art_source``.

    Examples::

        >>> canonical_source("Reuters")
        'reuters'
        >>> canonical_source("www.reuters.com")
        'reuters'
        >>> canonical_source("Thomson Reuters")
        'reuters'
        >>> canonical_source("Agence France-Presse")
        'afp'
        >>> canonical_source("AFP")
        'afp'
        >>> canonical_source("unknown-outlet.example.com")
        'unknown-outlet.example.com'
    """
    raw = name_or_domain.strip()

    # 1. Direct lookup
    key = raw.lower()
    if key in REGISTRY:
        return REGISTRY[key]

    # 2a. URL with scheme — treat as domain after stripping scheme+path
    if _HAS_SCHEME.match(raw):
        domain_key = _normalise_domain(raw)
        if domain_key in REGISTRY:
            return REGISTRY[domain_key]
        return domain_key

    # 2b. Bare domain (no scheme, no spaces)
    if _is_domain(raw):
        domain_key = _normalise_domain(raw)
        if domain_key in REGISTRY:
            return REGISTRY[domain_key]
        # Return the normalised domain as-is — still deduplicated across www/m variants
        return domain_key

    # 3. Name normalisation
    name_key = _normalise_name(raw)
    if name_key in REGISTRY:
        return REGISTRY[name_key]

    # 4. Unknown — return normalised name so at least casing/noise is consistent
    return name_key if name_key else key


def _token_overlap(a: str, b: str) -> float:
    """Jaccard overlap on word tokens between two normalised strings.

    Used by ``alias_clusters`` as a structural similarity signal when a registry
    entry is unavailable — without any I/O or RNG.
    """
    ta = set(a.split())
    tb = set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _canon_pairs(sources: list[str]) -> Iterator[tuple[int, int]]:
    """Yield (i, j) pairs whose canonical ids are equal — they are confirmed aliases."""
    canon = [canonical_source(s) for s in sources]
    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            if canon[i] == canon[j]:
                yield (i, j)


def _token_pairs(sources: list[str], threshold: float) -> Iterator[tuple[int, int]]:
    """Yield (i, j) pairs with high normalised-name token overlap (no-registry fallback).

    This catches "Reuters UK" ↔ "Reuters US" → same originator when neither appears
    in the registry, without any embedding or I/O.  Only fires on non-domain strings,
    since domain overlap is less meaningful (e.g. "news.co.uk" ↔ "sport.co.uk").
    """
    normed = [_normalise_name(s) if not _is_domain(s) else s for s in sources]
    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            if _token_overlap(normed[i], normed[j]) >= threshold:
                yield (i, j)


def _union_find(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    """Return connected components from edges — same algorithm as corroborate._components."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def alias_clusters(
    sources: list[str],
    token_threshold: float = 0.60,
) -> list[list[str]]:
    """Group a list of source strings into equivalence classes of aliases.

    Two strings are considered aliases if:
      - their ``canonical_source`` ids are equal (registry-backed), OR
      - their normalised-name token overlap clears ``token_threshold`` (structural fallback).

    Returns a list of groups; each group is a list of the original source strings.
    Singleton groups are included so the caller can iterate over ALL sources.

    ``token_threshold=0.60`` is intentionally loose: it will catch "Reuters UK" /
    "Reuters US" but not "The Times" / "The Guardian".  DRAFT — revisit with cauri.

    Example::

        >>> alias_clusters(["Reuters", "reuters.com", "Thomson Reuters", "AFP", "Agence France-Presse"])
        [['Reuters', 'reuters.com', 'Thomson Reuters'], ['AFP', 'Agence France-Presse']]
    """
    if not sources:
        return []
    n = len(sources)
    edges: list[tuple[int, int]] = []
    edges.extend(_canon_pairs(sources))
    edges.extend(_token_pairs(sources, token_threshold))
    # Deduplicate edges (both generators may emit the same pair)
    edges = list(dict.fromkeys(edges))
    groups_idx = _union_find(n, edges)
    return [[sources[i] for i in g] for g in groups_idx]
