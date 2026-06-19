"""Country-inference heuristics for curation (#291).

A neutral leaf (no LLM, no maat-layer deps) so both the serving layer (``serving.feed`` curation)
and the agent layer (``agents.geotag_agent``) share the EXACT same TLD/language country guess
without ``agents`` reaching into a ``serving`` private. Curation-only — never a veracity signal.

The LLM gap-filler for clusters this heuristic can't place lives in ``pipeline.geotag.llm_country``.
"""

from __future__ import annotations

import json
from typing import Any

# Claim-language → country (non-English only; English doesn't narrow to one country).
_LANG_TO_COUNTRY: dict[str, str] = {
    "ar": "SA", "de": "DE", "es": "ES", "fr": "FR", "hi": "IN", "it": "IT",
    "ja": "JP", "ko": "KR", "nl": "NL", "pl": "PL", "pt": "BR", "ru": "RU",
    "sv": "SE", "tr": "TR", "uk": "UA", "zh": "CN",
}

# TLD → country (for bare-domain sources like "bbc.co.uk", "lemonde.fr")
_TLD_TO_COUNTRY: dict[str, str] = {
    "co.uk": "GB", "uk": "GB", "fr": "FR", "de": "DE", "it": "IT", "es": "ES",
    "nl": "NL", "pt": "PT", "com.br": "BR", "br": "BR", "au": "AU", "ca": "CA",
    "cn": "CN", "jp": "JP", "kr": "KR", "ru": "RU", "co.in": "IN", "in": "IN",
    "co.za": "ZA", "za": "ZA", "ng": "NG", "ke": "KE", "eg": "EG",
    "ar": "AR", "mx": "MX", "tr": "TR", "se": "SE", "no": "NO", "pl": "PL",
}


def _as_list(v: Any) -> list:
    """Parse a JSON column that may already be decoded (asyncpg returns Python objects)."""
    if isinstance(v, str):
        return json.loads(v)
    if v is None:
        return []
    return list(v)


def source_country(source: str) -> str:
    """Guess ISO-3166-1 alpha-2 country from a source domain. Empty string if unknown."""
    s = (source or "").lower().strip()
    if not s:
        return ""
    # Longest-match TLD suffix first
    for tld, country in sorted(_TLD_TO_COUNTRY.items(), key=lambda x: -len(x[0])):
        if s.endswith("." + tld) or s == tld:
            return country
    return ""


def infer_country(
    claims: list[dict[str, Any]],
    article_meta: dict[str, dict[str, Any]],
    originators_raw: Any,
) -> str:
    """Best-effort country inference for curation — not a veracity signal.

    Order of preference:
    1. Source-domain TLD from independent originator articles.
    2. Language of the claims (non-English only — English doesn't narrow to a country).
    Falls back to "" (unknown) — curate() treats unknown country as uncapped.
    """
    for grp in _as_list(originators_raw):
        for aid in grp:
            art = article_meta.get(aid) or {}
            c = source_country(art.get("source") or "")
            if c:
                return c
    for claim in claims:
        lang = (claim.get("language") or "").lower()[:2]
        if lang and lang != "en":
            c = _LANG_TO_COUNTRY.get(lang, "")
            if c:
                return c
    return ""
