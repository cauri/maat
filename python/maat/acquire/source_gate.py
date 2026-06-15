"""Source gate (#sources) — keep only credible PUBLISHERS; drop noise before it becomes an article.

A news platform must not analyse wikipedia pages, SEO listicles, random social posts, or non-news
videos. This gate runs at acquisition, on each candidate's domain + headline, BEFORE the body is
fetched or `article.ingested` is published — so non-news never enters the pipeline and never
pollutes the reputation page.

Two layers:
  1. a free deterministic prefilter that drops obvious non-news (encyclopedias, social, UGC) —
     but NOT YouTube, since many news outlets publish there (judged below);
  2. an LLM classifier (cheap model, cached per domain) for everything else.

Policy (cauri): accept news outlets + official primary sources + reputable institutions; drop the
rest. The classifier prompt is editable in the console (/prompts, key "source_gate") and follows
the project prompt template; its CONTENT is co-designed with cauri.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from maat.providers.seam import claude_complete

# Editable in /prompts (key "source_gate"). Follows the project prompt template.
PROMPT = r"""ROLE
You are a source-vetting specialist for a news platform. Your role is to decide whether a web
result comes from a credible publisher worth analysing — judging the publisher itself, never the
topic, importance, or truth of the content.

GOALS
- Pass only credible publishers: genuine news outlets, official primary sources, and reputable institutions.
- Discard reference pages, social and user-generated content, SEO and content farms before analysis.
- Accept a real news outlet whatever the topic, in any country or language.

PROCESS
1. Identify the publisher from the domain (and, for a video or social URL, the channel or author).
2. Classify it as one of:
   - news: a journalism outlet — newspaper, wire service, magazine, broadcaster, or trade press.
   - primary: an organisation's own official release about itself — a government body, central bank, regulator, court, or a company's own newsroom / press release.
   - institution: a reputable research institute, think tank, IGO, university, or standards body publishing original analysis or reports.
   - other: none of the above.
3. For a video or social platform (e.g. YouTube), do not reject on the platform alone — news outlets publish there. Accept only when the channel or author is clearly a news organisation; otherwise classify it "other".
4. Set accept to true for news, primary, or institution; false for other.

GUIDELINES
- Judge the publisher, not the topic — a real outlet covering a light or local story is still news.
- Accept clearly journalistic domains in any region or language, even ones you do not recognise.
- Prefer rejecting when a domain genuinely looks like a blog, content farm, or aggregator rather than a publisher.
- Set "outlet" to the human-readable publisher name (e.g. "BBC News", "European Central Bank", "Reuters").

GUARDRAILS
- Do not assess whether the content is true or important — that is not your task here.
- Never accept a source only because the headline is newsworthy; the publisher decides, not the topic.
- Reject encyclopedias, wikis, dictionaries, glossaries, and how-to pages.
- Reject social posts, forums, and user-generated content that is not from a news organisation.
- Reject SEO content farms, listicles, affiliate or product pages, and link aggregators.

OUTPUT FORMAT
- A single JSON object, nothing else:
  { "accept": true|false, "kind": "news"|"primary"|"institution"|"other", "outlet": string }

CONTEXT
DOMAIN
{domain}

HEADLINE
{headline}

CHANNEL
{channel}

EXAMPLES
DOMAIN: reuters.com — HEADLINE: ECB holds rates as inflation cools
Output: {"accept": true, "kind": "news", "outlet": "Reuters"}

DOMAIN: en.wikipedia.org — HEADLINE: Inflation
Output: {"accept": false, "kind": "other", "outlet": ""}

DOMAIN: youtube.com — HEADLINE: BBC News — the week explained
Output: {"accept": true, "kind": "news", "outlet": "BBC News"}

DOMAIN: ecb.europa.eu — HEADLINE: Monetary policy decisions
Output: {"accept": true, "kind": "primary", "outlet": "European Central Bank"}
"""

# Obvious non-news, dropped for free (no LLM). YouTube is deliberately ABSENT — news outlets
# publish there, so it goes to the classifier (judged by channel / headline).
_HARD_REJECT: tuple[str, ...] = (
    "wikipedia.org", "wikimedia.org", "wiktionary.org", "fandom.com", "wikihow.com",
    "reddit.com", "x.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com",
    "pinterest.com", "quora.com", "medium.com", "tumblr.com", "linkedin.com", "imgur.com",
)


def _root(domain: str) -> str:
    return (domain or "").lower().removeprefix("www.").strip()


def prefiltered_reject(domain: str) -> bool:
    """True if the domain is obvious non-news we can drop without an LLM call. Pure."""
    d = _root(domain)
    return any(d == h or d.endswith("." + h) for h in _HARD_REJECT)


@dataclass(frozen=True)
class Verdict:
    accept: bool
    kind: str   # "news" | "primary" | "institution" | "other"
    outlet: str


def parse_verdict(text: str) -> Verdict | None:
    """Pure parse of the classifier's JSON reply; None if unusable."""
    try:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        d = json.loads(text[start : end + 1])
        return Verdict(
            accept=bool(d.get("accept")),
            kind=str(d.get("kind") or "other"),
            outlet=str(d.get("outlet") or ""),
        )
    except Exception:  # noqa: BLE001 - bad JSON / shape
        return None


def classify(domain: str, headline: str, channel: str = "", *, prompt: str = PROMPT) -> Verdict:
    """LLM source classification. FAIL-OPEN (accept) on any error so a glitch never drops news."""
    filled = (
        prompt.replace("{domain}", domain or "")
        .replace("{headline}", headline or "")
        .replace("{channel}", channel or "")
    )
    try:
        reply = claude_complete(filled, max_tokens=150)
        v = parse_verdict(reply.text)
        return v if v is not None else Verdict(True, "news", _root(domain))
    except Exception:  # noqa: BLE001 - provider/network error → fail open
        return Verdict(True, "news", _root(domain))


def accept_source(
    domain: str,
    headline: str,
    channel: str = "",
    *,
    prompt: str = PROMPT,
    known_good: frozenset[str] = frozenset(),
    cache: dict[str, Verdict] | None = None,
) -> Verdict:
    """Decide whether a candidate source is acceptable: prefilter → known-good → cache → classify.

    ``known_good`` is the set of domains already in the corpus (previously accepted, so news);
    ``cache`` dedupes within a run so each domain is classified at most once.
    """
    d = _root(domain)
    if cache is not None and d in cache:
        return cache[d]
    if prefiltered_reject(d):
        v = Verdict(False, "other", "")
    elif d in known_good:
        v = Verdict(True, "news", d)  # already in the corpus → previously accepted
    else:
        v = classify(domain, headline, channel, prompt=prompt)
    if cache is not None:
        cache[d] = v
    return v
