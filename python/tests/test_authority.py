"""Tiered authority (#434) — seek the claim's source of truth before counting corroboration.

cauri's design, pinned end to end: the authority leg proposes tier-1/2 primary sources (the paper,
the filing, the court record, the institution's own release); they pass the SAME NLI + grounding
gates as any evidence; an accepted one marks its URL primary for the fold (the primary lift +
rescue of a lone central claim), and a grounded authority CONTRADICTION disputes the claim even
when outlets corroborate — on a central claim, disqualifying the article. The general web-search
leg's behaviour must be byte-identical when the authority seam is None.
"""

from __future__ import annotations

from maat.acquire.fetch import FetchedPage
from maat.learning.article_credibility import ArticleClaim, score_article
from maat.pipeline.analyse import Citation, analyse_article, check_one_claim
from maat.pipeline.authority import AUTHORITY_SEARCH_PROMPT, parse_authority_citations
from maat.pipeline.claim import Claim
from maat.pipeline.corroborate import ClaimRow, corroborate_fixed

_GOLD = "The central bank sold half its gold reserves"
_URL = "https://www.chronicle.example/gold-story"

_BODY = (
    "The central bank secretly sold half its gold, the insider added. "
    "Officials did not respond to questions on Tuesday."
)
# The authority's own document (tier 1) — asserts the fact.
_FILING_URL = "https://centralbank-registry.example/q2-filing"
_FILING_QUOTE = "the bank confirms the sale of half its gold reserves"
_FILING_BODY = f"In its quarterly filing {_FILING_QUOTE} as part of a planned rebalancing."
# The authority's own document — DENIES the fact.
_DENIAL_URL = "https://centralbank-registry.example/statement"
_DENIAL_QUOTE = "the central bank denied selling any gold reserves"
_DENIAL_BODY = f"In a statement {_DENIAL_QUOTE} this year or any other."
# An ordinary news outlet corroborating (tier 0, the general leg).
_BBC_URL = "https://bbc.co.uk/gold"
_BBC_QUOTE = "The central bank has sold about half of its gold reserves"
_BBC_BODY = f"{_BBC_QUOTE} over recent months, the governor said."

_PAGES = {
    _URL: _BODY, _FILING_URL: _FILING_BODY, _DENIAL_URL: _DENIAL_BODY, _BBC_URL: _BBC_BODY,
}


def _fetch(url):
    body = _PAGES.get(url)
    return FetchedPage(body=body, title="t", image=None, date="2026-07-17") if body else None


def _nli(premise, hypothesis):
    both = (premise + " " + hypothesis).lower()
    if "denied" in both or "denies" in both:
        return ("contradiction", 0.9)
    if "gold" in premise.lower() and "gold" in hypothesis.lower():
        return ("entailment", 0.95)
    return ("neutral", 0.8)


def _extract(body, **kw):
    if body == _BODY:
        return [Claim(text=_GOLD, voice="attributed", speaker="an insider", in_headline=True,
                      evidence_span="The central bank secretly sold half its gold")]
    return []


def _classify(claims, **kw):
    return [c.model_copy(update={"kind": "fact"}) for c in claims]


def _analyse(*, authority=None, web=None, nli=_nli):
    return analyse_article(
        _URL,
        reputation={},
        corpus_lookup=lambda texts: [None] * len(texts),  # novel claim → the live path
        fetch=_fetch,
        extract=_extract,
        classify=_classify,
        extremity_of=lambda _t: "extraordinary",  # central + extraordinary = the disqualifying tier
        embed=lambda ts: [[1.0, 0.0] for _ in ts],
        language_of=lambda _t: "en",
        web_search=web,
        authority_search=authority,
        nli=nli,
    )


def _authority_returning(cits):
    def authority_search(claim_texts, own_domain, deep=False):
        return [list(cits) for _ in claim_texts]
    return authority_search


# --- the parser enforces the tier guardrail ------------------------------------------------------


def test_parse_authority_citations_keeps_only_tiers_1_and_2():
    text = """Here you go: {"1": [
        {"url": "https://a.example/p", "domain": "a.example", "quote": "q1", "tier": 1},
        {"url": "https://b.example/s", "domain": "b.example", "quote": "q2", "tier": 2},
        {"url": "https://news.example/x", "domain": "news.example", "quote": "q3", "tier": 3},
        {"url": "https://c.example/y", "domain": "c.example", "quote": "q4"},
        {"url": "https://d.example/z", "domain": "d.example", "quote": "q5", "tier": "junk"},
        "not-a-dict"
    ]}"""
    got = parse_authority_citations(text, 1)[0]
    assert [(c.domain, c.tier) for c in got] == [("a.example", 1), ("b.example", 2)]
    # the guardrail lives in CODE, not just the prompt: tier 3 / missing / junk never get through
    assert parse_authority_citations("no json here", 2) == [[], []]
    assert parse_authority_citations('{"nope": 1}', 1) == [[]]


def test_citation_tier_defaults_to_zero_for_the_general_leg():
    assert Citation(url="u", domain="d", quote="q").tier == 0


# --- an accepted authority citation IS a primary source ------------------------------------------


def test_corroborate_fixed_primary_urls_flags_has_primary():
    rows = [
        ClaimRow(id="own", text=_GOLD, article_id="analysed", source="chronicle.example"),
        ClaimRow(id="c1", text=_FILING_QUOTE, article_id=_FILING_URL,
                 source="centralbank-registry.example"),
    ]
    bodies = {"analysed": _BODY, _FILING_URL: _FILING_BODY}
    without = corroborate_fixed(rows, bodies, "extraordinary")
    assert not without.has_primary  # the registry domain matches NO name heuristic — that's the gap
    with_flag = corroborate_fixed(rows, bodies, "extraordinary", primary_urls={_FILING_URL})
    assert with_flag.has_primary
    assert with_flag.confidence > without.confidence  # the primary lift is earned, not asserted


def test_authority_support_rescues_a_lone_extraordinary_central_claim():
    """The exact scenario cauri named: an extraordinary claim (a bank selling half its gold) that
    no outlet corroborates, but whose authority's own filing asserts it. Without the leg the claim
    is lone-central-extraordinary → the article DISQUALIFIES; with it, the primary carries it."""
    baseline = _analyse(authority=None, web=None)
    assert baseline.score.band == "disqualified"

    res = _analyse(authority=_authority_returning(
        [Citation(url=_FILING_URL, domain="centralbank-registry.example",
                  quote=_FILING_QUOTE, tier=1)]
    ))
    gold = next(r for r in res.facts if r.claim.text == _GOLD)
    assert gold.has_primary
    assert not gold.primary_contradicted
    assert res.score.band != "disqualified"
    assert res.live is not None and res.live.authority_corroborated == 1


def test_authority_contradiction_disputes_despite_outlet_corroboration():
    """The tier ladder's teeth: the authority's own statement DENIES the claim while a real outlet
    corroborates it. Repetition must lose to the primary document — the claim is disputed, worded
    as such, and the article (central claim) is disqualified with the primary-source reason."""
    def web(claim_texts, own_domain, deep=False):
        return [[Citation(url=_BBC_URL, domain="bbc.co.uk", quote=_BBC_QUOTE)]
                for _ in claim_texts]

    res = _analyse(
        authority=_authority_returning(
            [Citation(url=_DENIAL_URL, domain="centralbank-registry.example",
                      quote=_DENIAL_QUOTE, tier=1)]
        ),
        web=web,
    )
    gold = next(r for r in res.facts if r.claim.text == _GOLD)
    assert gold.primary_contradicted and gold.disputed
    assert gold.verdict == "Disputed — contradicted by the primary source"
    assert res.score.band == "disqualified"
    assert "a central claim is contradicted by the primary source" in res.score.why
    assert res.live is not None and res.live.authority_contradicted == 1


def test_a_tier0_contradiction_stays_conservative():
    """The pre-#434 behaviour is preserved for the GENERAL leg: an ordinary source contradicting is
    only a dispute when nothing corroborates. Authority strength belongs to tier-1/2 alone."""
    def web(claim_texts, own_domain, deep=False):
        return [[Citation(url=_DENIAL_URL, domain="denials.example", quote=_DENIAL_QUOTE),
                 Citation(url=_BBC_URL, domain="bbc.co.uk", quote=_BBC_QUOTE)]
                for _ in claim_texts]

    res = _analyse(authority=None, web=web)
    gold = next(r for r in res.facts if r.claim.text == _GOLD)
    assert not gold.disputed and not gold.primary_contradicted


def test_check_one_claim_carries_the_authority_leg():
    claim = Claim(text=_GOLD, voice="attributed", speaker="an insider",
                  evidence_span="The central bank secretly sold half its gold")
    reading, _rated = check_one_claim(
        claim, body=_BODY, source="chronicle.example", url=_URL, reputation={},
        authority_search=_authority_returning(
            [Citation(url=_FILING_URL, domain="centralbank-registry.example",
                      quote=_FILING_QUOTE, tier=1)]
        ),
        nli=_nli, fetch=_fetch, extremity_of=lambda _t: "extraordinary",
    )
    assert reading.has_primary
    assert reading.verdict != "Only this source so far"


# --- the overall score's primary-contradiction disqualifier --------------------------------------


def test_score_article_disqualifies_on_central_primary_contradiction():
    claims = [ArticleClaim(confidence=0.8, independent_originators=3, has_primary=False,
                           extremity="notable", central=True, primary_contradicted=True)]
    score = score_article(claims)
    assert score.band == "disqualified"
    assert "a central claim is contradicted by the primary source" in score.why
    # a NON-central primary contradiction does not disqualify the whole article
    ok = score_article([
        ArticleClaim(confidence=0.8, independent_originators=3, has_primary=False, central=True),
        ArticleClaim(confidence=0.2, independent_originators=1, has_primary=False,
                     primary_contradicted=True),
    ])
    assert ok.band != "disqualified"


# --- the prompt ships as DRAFT, registered and editable ------------------------------------------


def test_authority_prompt_is_registered_as_draft_with_its_placeholders():
    from maat.prompts import PROMPTS_BY_KEY

    entry = PROMPTS_BY_KEY["authority_search"]
    assert entry["status"] == "draft"  # cauri reviews before it is treated as locked
    assert entry["default"] == AUTHORITY_SEARCH_PROMPT
    for ph in entry["placeholders"]:
        assert ph in AUTHORITY_SEARCH_PROMPT, f"declared placeholder {ph} missing from the seed"
    # the template sections cauri's prompt standard requires
    for section in ("# ROLE", "# GOALS", "# INSTRUCTIONS", "# GUIDELINES", "# GUARDRAILS",
                    "# OUTPUT FORMAT", "# CONTEXT"):
        assert section in AUTHORITY_SEARCH_PROMPT


# --- the engine leg (grounding agent) -------------------------------------------------------------


def test_engine_authority_verdict_supported_and_contradicted(monkeypatch):
    import maat.agents.grounding_agent as agent

    def fake_ws(prompt, tools=None, model=None):
        return [{"type": "text", "text":
                 '{"1": [{"url": "%s", "domain": "centralbank-registry.example", '
                 '"quote": "%s", "tier": 1}]}' % (_FILING_URL, _FILING_QUOTE)}]

    monkeypatch.setattr("maat.providers.seam.claude_web_search", fake_ws)
    monkeypatch.setattr("maat.pipeline.nli.available", lambda: True)
    monkeypatch.setattr("maat.pipeline.nli.classify_pair", _nli)
    monkeypatch.setattr("maat.acquire.fetch.fetch_page", lambda u, fast=True: _fetch(u))

    got = agent._authority_verdict(_GOLD, AUTHORITY_SEARCH_PROMPT)
    assert got == ("supported", _FILING_QUOTE, "centralbank-registry.example")

    def fake_ws_denial(prompt, tools=None, model=None):
        return [{"type": "text", "text":
                 '{"1": [{"url": "%s", "domain": "centralbank-registry.example", '
                 '"quote": "%s", "tier": 1}]}' % (_DENIAL_URL, _DENIAL_QUOTE)}]

    monkeypatch.setattr("maat.providers.seam.claude_web_search", fake_ws_denial)
    got = agent._authority_verdict(_GOLD, AUTHORITY_SEARCH_PROMPT)
    assert got == ("contradicted", _DENIAL_QUOTE, "centralbank-registry.example")


def test_engine_authority_verdict_requires_the_nli_gate(monkeypatch):
    """No NLI model → no authority verdicts. The engine must never trust the search model's own
    claim→source mapping — same rule as the Analyse path."""
    import maat.agents.grounding_agent as agent

    monkeypatch.setattr("maat.pipeline.nli.available", lambda: False)
    assert agent._authority_verdict(_GOLD, AUTHORITY_SEARCH_PROMPT) is None
