"""Evidence-gated ownership collapse (#425) — co-ownership is a hypothesis, not a merge.

cauri's design, pinned: "co-owned outlets that make the same false claims — clear evidence of news
laundering. Two outlets that do not share the same news (the Condé Nast pattern) need not be
collapsed. State control is just like any other ownership." Both failure directions have shipped
bugs before (historical-owner chaining, index-fund P127 leakage — each silently SUPPRESSED real
corroboration), so these tests pin the direction of every doubt: independence.
"""

from __future__ import annotations

from maat.pipeline.ownership import evidenced_ownership

_CONDE = {"vogue.com": "Condé Nast", "wired.com": "Condé Nast"}
_RING = {"ring1.com": "X Corp", "ring2.com": "X Corp"}


def _fact(fact, sources, *, corrected=False, n=2):
    return {"fact": fact, "sources": sources, "independent_originators": n,
            "has_primary": False, "corrected": corrected}


def test_co_owned_without_shared_output_stays_independent():
    """The Condé Nast pattern: common owner, independent newsrooms, no shared claims — they must
    NOT collapse. Blanket collapse here HIDES real corroboration (two genuinely independent
    reports read as one)."""
    hist = [
        _fact("a fashion story", ["vogue.com", "reuters.com"]),
        _fact("a tech story", ["wired.com", "apnews.com"]),
    ]
    assert evidenced_ownership(_CONDE, hist) == {}


def test_shared_false_claim_alone_collapses_the_pair():
    """The strongest signal: commonly owned outlets carrying the SAME claim that resolved REFUTED.
    One is enough — that is what news laundering looks like."""
    hist = [_fact("the false claim", ["ring1.com", "ring2.com"], corrected=True)]
    got = evidenced_ownership(_RING, hist)
    assert got == {"ring1.com": "X Corp", "ring2.com": "X Corp"}


def test_repeated_shared_output_collapses_without_a_refutation():
    """Refutations are rare today (no automated contradiction detector feeds outcomes), so plain
    same-fact co-occurrence at the min_shared bar is the workable tier — without it the ownership
    signal would silently turn off."""
    hist = [
        _fact("shared claim A", ["ring1.com", "ring2.com"]),
        _fact("shared claim B", ["ring1.com", "ring2.com"]),
    ]
    assert evidenced_ownership(_RING, hist) != {}
    # …but a single co-occurrence is below the bar (one shared story is just news)
    assert evidenced_ownership(_RING, hist[:1]) == {}


def test_shared_output_without_shared_ownership_never_collapses():
    """Two independent outlets carrying the same facts is CORROBORATION — the entire product.
    Evidence gating must never turn agreement itself into a merge."""
    hist = [
        _fact("shared claim A", ["ring1.com", "vogue.com"]),
        _fact("shared claim B", ["ring1.com", "vogue.com"]),
        _fact("shared claim C", ["ring1.com", "vogue.com"], corrected=True),
    ]
    assert evidenced_ownership({**_CONDE, **_RING}, hist) == {}


def test_a_blanket_group_splits_into_its_evidenced_subgroup():
    """Owner group of three where only two share output: the evidenced pair keeps the label, the
    third member drops out and counts independent — a group can SPLIT."""
    auto = {"a.com": "G", "b.com": "G", "c.com": "G"}
    hist = [
        _fact("shared 1", ["a.com", "b.com"]),
        _fact("shared 2", ["a.com", "b.com"]),
        _fact("solo story", ["c.com", "reuters.com"]),
    ]
    got = evidenced_ownership(auto, hist)
    assert got == {"a.com": "G", "b.com": "G"}


def test_raw_source_strings_are_canonicalised_before_pairing():
    """Trajectory sources are RAW strings while the ownership map is canonical-keyed — the exact
    silent-miss class an earlier bug shipped. www/variant forms must still pair."""
    hist = [
        _fact("shared A", ["www.ring1.com", "ring2.com"]),
        _fact("shared B", ["ring1.com", "www.ring2.com"]),
    ]
    assert evidenced_ownership(_RING, hist) != {}


def test_empty_inputs_are_empty_not_errors():
    assert evidenced_ownership({}, [_fact("x", ["a.com", "b.com"])]) == {}
    assert evidenced_ownership(_RING, []) == {}
