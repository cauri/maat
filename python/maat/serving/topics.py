"""NL-interest → acquisition topics + story matcher (P5, issue #50).

A user expresses an interest in natural language ("European monetary policy",
"West African politics", "semiconductor supply chains") and this module does two
things:

1. ``parse_interest(interest)`` — pure keyword extraction that maps a free-text
   interest string to a ``TopicSpec`` (structured terms, optional GDELT filters,
   acquisition query string).  This pure path is the testable core and the default
   runtime path.  An optional LLM path (behind a DRAFT prompt) can enrich it; it
   falls back to the pure path on error, so the pipeline never stalls.

2. ``story_matches(story, topics)`` — pure predicate.  Given a story dict
   (``title``, ``body``, ``language``, ``country``, ``source``) and a list of
   ``TopicSpec`` objects, returns ``True`` if the story is relevant to any of them.

Architecture notes
- Both functions are pure / deterministic: no I/O, no randomness, no imports
  from ``providers/`` at module load time.
- The LLM path is gated behind an explicit ``use_llm=True`` parameter that
  callers must opt into; the pure path is the default everywhere (tests, curation,
  the clock).
- The DRAFT prompt is marked per the project convention and must NOT be activated
  without cauri review.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from functools import lru_cache

# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopicSpec:
    """Structured representation of a user interest.

    ``terms``       — the key words/phrases that characterise the interest (used for
                      acquisition queries and story matching).
    ``sourcelang``  — optional GDELT ``sourcelang:`` filter (e.g. ``"French"``).
    ``sourcecountry`` — optional GDELT ``sourcecountry:`` filter (e.g. ``"GH"``).
    ``query``       — the canonical GDELT/acquisition query string derived from terms.
    ``raw``         — the original interest string (for audit / display).
    """

    terms: tuple[str, ...]
    raw: str
    query: str
    sourcelang: str | None = None
    sourcecountry: str | None = None


# ---------------------------------------------------------------------------
# Stop-words (English; minimal set tuned for news-topic extraction)
# ---------------------------------------------------------------------------

_STOP: frozenset[str] = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
        "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
        "being", "have", "has", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "shall", "can", "into", "about",
        "over", "under", "through", "between", "among", "across", "within",
        "without", "during", "before", "after", "up", "down", "out", "off",
        "its", "it", "this", "that", "these", "those", "i", "we", "you",
        "he", "she", "they", "his", "her", "their", "our", "my", "your",
        "as", "so", "not", "no", "nor", "yet", "both", "either", "neither",
        "just", "also", "very", "more", "most", "such", "other", "than",
    }
)

# Noun-adjacent suffixes that carry signal when kept whole (e.g. "policy", "chain").
# We always keep tokens that are >= MIN_TOKEN_LEN after stripping punctuation.
_MIN_TOKEN_LEN = 3


def _tokenise(text: str) -> list[str]:
    """Lower-case, strip punctuation, drop stop-words and short tokens."""
    text = text.lower()
    # Replace hyphens with space so "supply-chains" → ["supply", "chains"]
    text = text.replace("-", " ")
    tokens = text.split()
    out: list[str] = []
    for tok in tokens:
        tok = tok.strip(string.punctuation)
        if len(tok) < _MIN_TOKEN_LEN:
            continue
        if tok in _STOP:
            continue
        out.append(tok)
    return out


def _bigrams(tokens: list[str]) -> list[str]:
    """Adjacent pairs that both survive the stop-word filter."""
    return [f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens) - 1)]


# ---------------------------------------------------------------------------
# Pure extraction core
# ---------------------------------------------------------------------------


def _pure_parse(interest: str) -> TopicSpec:
    """Deterministic keyword extraction — the testable, always-available core.

    Strategy:
    - Tokenise; keep unigrams that survive the stop-word filter.
    - Form bigrams from adjacent surviving tokens (preserves "monetary policy",
      "supply chains", "West Africa").
    - ``terms`` = bigrams + unigrams, deduplicated, order-preserving (bigrams first
      so the most specific phrases lead the query).
    - ``query`` = first three terms joined by spaces (GDELT boolean default is AND).
    """
    stripped = interest.strip()
    tokens = _tokenise(stripped)
    bigrams = _bigrams(tokens)

    seen: set[str] = set()
    terms: list[str] = []
    for phrase in bigrams + tokens:
        if phrase not in seen:
            seen.add(phrase)
            terms.append(phrase)

    # Guard: if everything was stop-words / too short, fall back to raw stripped
    if not terms:
        terms = [stripped] if stripped else []

    # Build a GDELT-friendly query.  The most specific phrase leads; we then add
    # unigrams that are NOT already substrings of the lead phrase (to avoid
    # duplicate tokens in the query string like "monetary policy monetary policy").
    top_bigram = next((t for t in terms if " " in t), None)
    unigrams = [t for t in terms if " " not in t]
    query_parts: list[str] = []
    if top_bigram:
        query_parts.append(top_bigram)
        # add extra unigrams not already covered by the bigram
        extra = [u for u in unigrams if u not in top_bigram.split()][:1]
        query_parts.extend(extra)
    else:
        query_parts.extend(unigrams[:3])
    query = " ".join(query_parts) if query_parts else stripped

    return TopicSpec(
        terms=tuple(terms),
        raw=stripped,
        query=query,
    )


# ---------------------------------------------------------------------------
# DRAFT prompt — LLM enrichment path (DO NOT ACTIVATE without cauri review)
# ---------------------------------------------------------------------------

# DRAFT prompt — flag for cauri review
_LLM_PROMPT_TEMPLATE = """\
# ROLE
You are a news-acquisition specialist.  Given a user's natural-language interest
description, extract the key topical terms that will retrieve the most relevant
articles from a global news search engine (GDELT).

# TASK
Return a JSON object with:
  "terms": list of 2–6 keyword phrases (most specific first), no stop-words
  "sourcelang": optional GDELT language code (e.g. "French") or null
  "sourcecountry": optional ISO-3166-1 alpha-2 (e.g. "GH" for Ghana) or null
  "query": the best single GDELT query string (space-joined key phrases)

# RULES
- Output ONLY valid JSON, no commentary.
- terms must be lowercase.
- Prefer noun phrases over individual words ("monetary policy" > "policy").
- If the interest is inherently regional, set sourcelang or sourcecountry.
- Never invent topics not implied by the interest.

# INTEREST
{interest}
"""


def _spec_from_reply(text: str, interest: str) -> TopicSpec:
    """Pure: parse a model's JSON reply into a TopicSpec. Raises on missing JSON so callers
    fall back to the deterministic parse. Shared by the judge and bulk enrichment paths."""
    import json as _json

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in reply")
    data = _json.loads(text[start : end + 1])
    raw_terms = [str(t).lower().strip() for t in data.get("terms", [])]
    terms = [t for t in raw_terms if t]
    query = str(data.get("query") or " ".join(terms[:3]) or interest.strip())
    return TopicSpec(
        terms=tuple(terms) if terms else _pure_parse(interest).terms,
        raw=interest.strip(),
        query=query,
        sourcelang=data.get("sourcelang") or None,
        sourcecountry=data.get("sourcecountry") or None,
    )


def _llm_parse(interest: str) -> TopicSpec:
    """LLM-enriched extraction via the JUDGE model — falls back to ``_pure_parse`` on any error.

    This path is disabled by default (callers must pass ``use_llm=True``).
    The prompt above is a DRAFT and must be reviewed by cauri before deployment.
    """
    from maat.providers.seam import claude_complete

    prompt = _LLM_PROMPT_TEMPLATE.replace("{interest}", interest.strip())
    try:
        reply = claude_complete(prompt, max_tokens=256)
        return _spec_from_reply(reply.text, interest)
    except Exception:  # noqa: BLE001 — network error, JSON error, key error: fall back
        return _pure_parse(interest)


# ---------------------------------------------------------------------------
# Hot-path topic enrichment (#189) — memoised bulk-model expansion for the feed filter.
#
# The personal-feed filter (serving.feed._filter_by_topics) runs on EVERY request, so it must
# never make a per-request LLM call. ``enriched_interest`` expands a reader's free-text interest
# into extra match terms via the BULK model, but caches the result per distinct interest — a busy
# feed therefore pays one call per NEW interest and zero thereafter. Enrichment only UNIONS terms
# onto the deterministic parse, so it can only add recall, never drop the keyword baseline.
# Gated by MAAT_TOPICS_LLM (read at the feed seam); OFF → the pure parse, unchanged.
# ---------------------------------------------------------------------------

# DRAFT prompt — flag for cauri review (in-platform agent prompt fed to the bulk model).
_TOPICS_ENRICH_PROMPT = """\
# ROLE
You expand a reader's news interest into match terms for filtering a news feed.

# TASK
Given one interest, return JSON only:
  {"terms": [4-8 lowercase phrases - close synonyms and the most newsworthy
             sub-topics a relevant headline or summary might use],
   "query": "<space-joined top phrases>"}

# RULES
- Stay strictly on-topic; never drift to adjacent-but-different subjects.
- Prefer noun phrases; include obvious synonyms and named sub-topics.
- Output ONLY valid JSON, no commentary.

# INTEREST
{interest}
"""
# DRAFT prompt — flag for cauri review


def _bulk_enrich(interest: str) -> TopicSpec:
    """One bulk-model expansion of ``interest``, UNIONed with the deterministic terms. Falls back
    to the pure parse on any error. Not cached itself — go through ``enriched_interest``."""
    from maat.providers.seam import mistral_complete

    base = _pure_parse(interest)
    try:
        reply = mistral_complete(_TOPICS_ENRICH_PROMPT.replace("{interest}", interest.strip()))
        spec = _spec_from_reply(reply.text, interest)
    except Exception:  # noqa: BLE001 — provider/network/JSON error: deterministic fallback
        return base
    merged = tuple(dict.fromkeys((*spec.terms, *base.terms)))  # LLM terms first, dedup, keep base
    return TopicSpec(
        terms=merged,
        raw=base.raw,
        query=spec.query or base.query,
        sourcelang=spec.sourcelang,
        sourcecountry=spec.sourcecountry,
    )


@lru_cache(maxsize=512)
def _enriched_cached(interest_key: str) -> TopicSpec:
    return _bulk_enrich(interest_key)


def enriched_interest(interest: str) -> TopicSpec:
    """Hot-path-safe LLM topic enrichment: memoised bulk-model expansion (one call per distinct
    interest), falling back to the pure parse on any error. Used by the feed filter when
    MAAT_TOPICS_LLM=1. Call ``_enriched_cached.cache_clear()`` to reset (tests/operator reload)."""
    norm = (interest or "").strip().lower()
    if not norm:
        return TopicSpec(terms=(), raw="", query="")
    return _enriched_cached(norm)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_interest(interest: str, *, use_llm: bool = False) -> TopicSpec:
    """Map a natural-language interest string → structured ``TopicSpec``.

    Pure (deterministic) by default.  Pass ``use_llm=True`` to attempt LLM
    enrichment (requires ``ANTHROPIC_API_KEY``); on any error it silently falls
    back to the pure path.

    Examples::

        parse_interest("European monetary policy")
        # TopicSpec(terms=('european monetary', 'monetary policy', 'european', 'monetary', 'policy'), ...)

        parse_interest("West African politics")
        # TopicSpec(terms=('west african', 'african politics', 'west', 'african', 'politics'), ...)
    """
    if not interest or not interest.strip():
        return TopicSpec(terms=(), raw="", query="")
    if use_llm:
        return _llm_parse(interest)
    return _pure_parse(interest)


# ---------------------------------------------------------------------------
# News-query generation (interest → recent-news search queries).
#
# The acquisition clock must NOT search the literal interest string — "fun and laughter" or "art"
# pull SEO/blog/listicle junk, not news. This turns each interest into a few NEWS-oriented search
# queries via the LLM (cauri-approved prompt; editable in /prompts as "acquire_queries"), falling
# back to the deterministic single query so acquisition never stalls.
# ---------------------------------------------------------------------------

# Active prompt — CONTENT co-designed with + approved by cauri. Editable in the console.
NEWS_QUERIES_PROMPT = """\
# ROLE
You turn a reader's interest into search queries that retrieve recent NEWS
articles from a global news index (GDELT) and web search.

# TASK
Given one interest, output JSON only:
  {"queries": [2-4 news search queries], "sourcelang": <GDELT lang or null>,
   "sourcecountry": <ISO-3166-1 alpha-2 or null>}

# RULES
- Every query must target NEWS - current events, decisions, launches, rulings,
  incidents, named people / organisations / places - NOT evergreen explainers,
  how-tos, listicles, definitions, or product/SEO pages.
- Frame for recent developments, not general knowledge. Examples:
    "artificial intelligence" -> ["AI regulation EU", "frontier model release",
                                   "AI chip export controls"]
    "scientific discovery"    -> ["new research findings", "space mission update",
                                   "clinical trial results"]
    "art"                     -> ["major museum exhibition", "art auction record",
                                   "cultural heritage ruling"]
    "fun and laughter"        -> ["comedy festival", "feel-good viral story",
                                   "entertainment award show"]
- 2-6 words per query, lowercase, no boolean operators.
- Set sourcelang/sourcecountry only if the interest is inherently regional.
- Output ONLY valid JSON, no commentary.

# INTEREST
{interest}
"""


def parse_news_queries(text: str, *, fallback: str, max_queries: int = 4) -> list[str]:
    """Pure parser: pull the news queries out of the model's JSON reply (testable, no I/O).

    Returns ``[fallback]`` if the reply has no usable queries, so acquisition never stalls on a
    malformed LLM response.
    """
    import json as _json

    try:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object")
        data = _json.loads(text[start : end + 1])
        queries = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
        return queries[:max_queries] or [fallback]
    except Exception:  # noqa: BLE001 - bad JSON / shape: fall back to the literal interest
        return [fallback]


def news_queries(
    interest: str, *, prompt: str = NEWS_QUERIES_PROMPT, max_queries: int = 4
) -> list[str]:
    """Interest -> up to ``max_queries`` recent-news search queries (one LLM call). Blocking (httpx).

    Falls back to the deterministic single-query parse on any error (no key, network, bad reply),
    so the acquisition clock always has something to search.
    """
    interest = (interest or "").strip()
    if not interest:
        return []
    fallback = _pure_parse(interest).query or interest
    try:
        from maat.providers.seam import claude_complete

        reply = claude_complete(prompt.replace("{interest}", interest), max_tokens=256)
        return parse_news_queries(reply.text, fallback=fallback, max_queries=max_queries)
    except Exception:  # noqa: BLE001 - provider/network error: deterministic fallback
        return [fallback]


def story_matches(story: dict, topics: list[TopicSpec], *, min_hits: int = 1) -> bool:
    """Return True if the story is relevant to at least one of the user's topics.

    Matching is case-insensitive substring search over the story's searchable text
    (title + body).  A story matches a topic if ``min_hits`` or more of that
    topic's terms appear in the text.

    Args:
        story:      dict with at least ``title`` (str) and/or ``body`` (str).
                    ``language`` and ``country`` are accepted but not used in the
                    pure matching path (reserved for future geo-filter extension).
        topics:     list of ``TopicSpec`` objects from ``parse_interest``.
        min_hits:   how many terms from a topic must appear for a match (default 1).
                    Callers wanting stricter matching can raise this.

    Returns:
        ``True`` if the story matches any topic, ``False`` otherwise.
    """
    if not topics:
        return False

    # Build the searchable text once
    title = (story.get("title") or "").lower()
    body = (story.get("body") or "").lower()
    haystack = f"{title} {body}"

    for topic in topics:
        if not topic.terms:
            continue
        hits = sum(1 for term in topic.terms if term in haystack)
        if hits >= min_hits:
            return True

    return False
