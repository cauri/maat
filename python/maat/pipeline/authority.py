"""Tiered authority — seek each claim's SOURCE OF TRUTH before counting corroboration (#434).

cauri's first level of fact checking: "if an article says a lab has found a cure for cancer, no
amount of looking for corroboration is as good as a press release from that lab, or better yet a
peer-reviewed paper. Secondary is corroboration from RELIABLE other outlets."

This module owns the AUTHORITY-SEEKING leg's prompt and parser. The search itself runs through the
same web-search seam as general corroboration (``serving.analyse.make_authority_search``), and its
citations go through the SAME gates — NLI entailment, quote grounding, source filters — as any
other evidence. The model only ever *proposes*; deterministic code + NLI decide. What differs is
the *target* (the authoritative primary, not press coverage) and the *weight* of what survives:

  * an accepted tier-1/2 citation marks its URL primary for the fold (``corroborate_fixed
    (primary_urls=…)``) — the primary lift, "Stated by the primary source", and the rescue of a
    lone central claim all follow from the existing, signed-off scoring model;
  * a grounded tier-1/2 citation that CONTRADICTS the claim disputes it even when outlets
    corroborate (the lab's own paper beats five stories repeating each other), and on a central
    claim it is DISQUALIFYING for the whole article.

The tier ladder (what the model is told to find, in order):
  1 — the primary document itself: a peer-reviewed paper, an official filing, a court record, the
      named institution's own release, report, or dataset;
  2 — an official statement by the relevant authority: its press office, an on-the-record
      spokesperson page, an official transcript;
  (everything else — news coverage, aggregators, wikis — is out of scope for this leg; the general
  corroboration pass already covers reporting.)
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

from maat.pipeline.analyse import Citation

log = logging.getLogger("maat.pipeline.authority")

# ⚠️ PROMPT REVIEW (cauri): NEW in-app agent prompt, shipped as DRAFT (cauri, 2026-07-17 — "ship as
# DRAFT via /prompts, listed in the PR for review"). Canonical seed; editable live via the prompt
# store like the extractor. Structured per docs/prompt-template.md.
AUTHORITY_SEARCH_PROMPT = r"""# ROLE

You are a primary-source researcher for a news-veracity engine. Given the factual claims of ONE article, you find, for each claim, the AUTHORITATIVE PRIMARY SOURCE that could settle it — the institution, document, or record the claim is ultimately about — and quote what it says, word for word. You do not judge whether the claim is true — you find what the authority itself published.

# GOALS

- For each claim, identify what kind of primary source would settle it (a paper, a filing, a court record, an official release or dataset, an on-the-record statement) and WHO would have issued it.
- Search for that specific source and return it with the exact sentence that bears on the claim — whether it supports the claim or says something different.

# INSTRUCTIONS

1. For each numbered claim, first determine its authority: the institution, body, or document the claim is ultimately about (the lab behind a study; the court behind a ruling; the agency behind a statistic; the company behind a filing; the government behind a policy).
2. Search SPECIFICALLY for that authority's own publication — its site, the journal, the docket, the official register — not for news coverage of it.
3. For each source found, copy ONE sentence VERBATIM from that page that bears on the claim — the exact words, no paraphrase, no ellipsis, no edits. Include it even when it CONTRADICTS the claim: what the authority actually says matters more than agreement.
4. Assign each source a tier:
   - tier 1 — the primary document itself: peer-reviewed paper, official filing, court record, the institution's own release, report, or dataset;
   - tier 2 — an official statement by the relevant authority: press office, on-the-record spokesperson page, official transcript.

# GUIDELINES

- The best source is the most upstream one: the paper over the university's press release, the press release over a ministry summary, the ruling over a lawyer's characterisation of it.
- A claim may have no findable primary source (an eyewitness account, an unnamed-officials story). Return an empty list for it — that is a correct and useful answer, not a failure.
- One authority's document may bear on several claims — reuse it wherever it applies.

# GUARDRAILS

- Never invent a URL, an institution, or a quote. Every quote must be text you actually read on the page at that URL; the engine re-fetches each page and discards any quote it cannot verify.
- Return ONLY tier-1 and tier-2 sources. Never news outlets, aggregators, encyclopedias, wikis, social media, or blogs — the general corroboration pass covers reporting.
- Never return the article's own publisher.
- Do not assess truth, tone, or bias; report what the authority published, supporting or not.

# OUTPUT FORMAT

A single JSON object and nothing else. Keys are the claim numbers as strings ("1" … "N"); each value is an array of objects {"url": string, "domain": string, "quote": string, "tier": 1 or 2}. The quote is verbatim. Use an empty array for claims with no findable primary source.

# CONTEXT

## ARTICLE PUBLISHER (never return its own pages)

{own_domain}

## CLAIMS

{claims}
"""


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.removeprefix("www.")
    except ValueError:
        return ""


def parse_authority_citations(text: str, n: int) -> list[list[Citation]]:
    """Per-claim AUTHORITY citations from the model's JSON reply (keys "1".."n").

    Same tolerance contract as ``serving.analyse.parse_citations`` (pulls the JSON out of prose,
    skips malformed entries, never raises) with one addition and one restriction: each entry carries
    ``tier``, and **only tiers 1 and 2 survive** — anything else (missing, 0, 3, junk) is dropped
    here, so no downstream path ever has to wonder whether an authority citation is really one.
    The parser enforces the guardrail; the prompt merely states it."""
    out: list[list[Citation]] = [[] for _ in range(n)]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return out
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return out
    if not isinstance(obj, dict):
        return out
    for key, items in obj.items():
        try:
            idx = int(str(key)) - 1
        except ValueError:
            continue
        if not (0 <= idx < n) or not isinstance(items, list):
            continue
        cits: list[Citation] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            quote = str(item.get("quote") or "").strip()
            try:
                tier = int(item.get("tier"))
            except (TypeError, ValueError):
                continue
            if not url or not quote or tier not in (1, 2):
                continue
            domain = str(item.get("domain") or "").strip().removeprefix("www.") or _domain_of(url)
            cits.append(Citation(url=url, domain=domain, quote=quote, tier=tier))
        out[idx] = cits
    return out
