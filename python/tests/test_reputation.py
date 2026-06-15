"""Reputation fold tests (P3, §5 — Issue #37).

Thorough tests over synthetic event histories. All tests are pure — no DB, no I/O, no LLM.
Convention: `cluster.corroborated` event dicts carry the fields the corroborate agent emits.
"""

from __future__ import annotations

from maat.learning.reputation import (
    SourceReputation,
    _norm_fact,
    _source_is_independent_originator,
    fold_reputation,
    reputation_by_source,
)


# ---------------------------------------------------------------------------
# Helpers for building synthetic event dicts
# ---------------------------------------------------------------------------

def _ev(
    fact: str,
    sources: list[str],
    originators: list[list[str]],
    *,
    has_primary: bool = False,
    extremity: str = "notable",
    confidence: float = 0.5,
    corrected: bool = False,
) -> dict:
    """Build a minimal `cluster.corroborated` event dict."""
    return {
        "fact": fact,
        "sources": sources,
        "originators": originators,
        "independent_originators": len(originators),
        "has_primary": has_primary,
        "extremity": extremity,
        "confidence": confidence,
        "corrected": corrected,
    }


# ---------------------------------------------------------------------------
# Unit: _norm_fact
# ---------------------------------------------------------------------------

def test_norm_fact_case_and_whitespace():
    assert _norm_fact("  Minister X  Resigned ") == "minister x resigned"
    assert _norm_fact("minister x resigned") == _norm_fact("MINISTER X RESIGNED")


def test_norm_fact_empty():
    assert _norm_fact("") == ""
    assert _norm_fact(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Unit: _source_is_independent_originator
# ---------------------------------------------------------------------------

def test_independent_originator_no_groups():
    assert not _source_is_independent_originator("Reuters", [])


def test_independent_originator_single_group_non_primary():
    # One group → everyone collapsed → only primary sources count as independent
    assert not _source_is_independent_originator("reuters.com", [["art-1", "art-2"]])


def test_independent_originator_single_group_primary():
    assert _source_is_independent_originator("ecb.europa.eu", [["art-1"]])


def test_independent_originator_multiple_groups():
    # Two groups → both sources are independent by convention
    assert _source_is_independent_originator("reuters.com", [["art-1"], ["art-2"]])
    assert _source_is_independent_originator("bbc.co.uk", [["art-3"], ["art-4"]])


# ---------------------------------------------------------------------------
# fold_reputation: empty / single-event cases
# ---------------------------------------------------------------------------

def test_fold_empty_stream_returns_empty():
    assert fold_reputation([]) == []


def test_fold_single_event_one_source():
    ev = _ev("A meteor hit the moon", ["nasa.gov"], [["art-1"]], has_primary=True)
    recs = fold_reputation([ev])
    assert len(recs) == 1
    r = recs[0]
    assert r.source == "nasa.gov"
    assert r.appearances == 1
    # nasa.gov is a primary source, one group → independent
    assert r.independent_appearances == 1
    assert r.primary_appearances == 1
    assert r.independent_rate == 1.0


def test_fold_single_event_multiple_sources_two_groups():
    # Two sources, two originator groups → both count as independent
    ev = _ev(
        "Rate cut announced",
        ["ft.com", "bloomberg.com"],
        [["art-1"], ["art-2"]],
    )
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    assert by["ft.com"].independent_appearances == 1
    assert by["bloomberg.com"].independent_appearances == 1


def test_fold_collapsed_sources_not_independent():
    # One group → sources collapsed into a single originator (wire/cascade)
    ev = _ev(
        "Minister resigned",
        ["afp.com", "thelocalnews.com"],
        [["art-1", "art-2"]],   # one group = collapsed
        extremity="notable",
    )
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    # Neither is a primary source → neither gets credit as independent
    assert by["afp.com"].independent_appearances == 0
    assert by["thelocalnews.com"].independent_appearances == 0


# ---------------------------------------------------------------------------
# fold_reputation: outcome tracking (truth-over-time, not consensus)
# ---------------------------------------------------------------------------

def test_confirmed_fact_credits_sources():
    # Fact appears twice (trajectory): starts thin, grows to 4 independent originators → confirmed.
    fact = "Economy grew 3% last quarter"
    early = _ev(fact, ["ft.com", "reuters.com"], [["art-1"], ["art-2"]], extremity="ordinary")
    late = _ev(fact, ["ft.com", "reuters.com", "wsj.com", "apnews.com"],
               [["art-1"], ["art-2"], ["art-3"], ["art-4"]], extremity="ordinary")
    recs = fold_reputation([early, late])
    by = reputation_by_source(recs)
    # All sources should have one confirmed fact
    for src in ["ft.com", "reuters.com", "wsj.com", "apnews.com"]:
        assert by[src].facts_confirmed == 1, f"{src} should have 1 confirmed fact"
        assert by[src].facts_refuted == 0


def test_refuted_fact_credits_sources_as_refuted():
    fact = "Alien life discovered"
    early = _ev(fact, ["tabloid.com"], [["art-1"]], extremity="extraordinary")
    # Correction event
    corrected = _ev(fact, ["tabloid.com"], [["art-1"]], extremity="extraordinary", corrected=True)
    recs = fold_reputation([early, corrected])
    by = reputation_by_source(recs)
    assert by["tabloid.com"].facts_refuted == 1
    assert by["tabloid.com"].facts_confirmed == 0


def test_unresolved_fact_is_counted_as_unresolved():
    # A fact that gains ground but never clears the confirm bar stays "corroborating" (not terminal).
    fact = "Treaty under negotiation"
    early = _ev(fact, ["bbc.co.uk"], [["art-1"]], extremity="notable")
    late = _ev(fact, ["bbc.co.uk", "guardian.com"], [["art-1"], ["art-2"]], extremity="notable")
    recs = fold_reputation([early, late])
    by = reputation_by_source(recs)
    # 2 independent originators < confirm_at=3 → corroborating (not terminal)
    for src in ["bbc.co.uk", "guardian.com"]:
        assert by[src].facts_unresolved == 1
        assert by[src].facts_confirmed == 0


# ---------------------------------------------------------------------------
# fold_reputation: confirmation_rate
# ---------------------------------------------------------------------------

def test_confirmation_rate_none_when_no_terminal_outcomes():
    fact = "Policy under review"
    ev = _ev(fact, ["reuters.com"], [["art-1"]], extremity="notable")
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    # Never reached confirm_at=3 originators → unconfirmed → no terminal outcome
    assert by["reuters.com"].confirmation_rate is None
    assert by["reuters.com"].outcome_n == 0


def test_confirmation_rate_perfect():
    # Source appears in 3 separate facts, all confirmed.
    events = []
    for i in range(3):
        fact = f"Confirmed fact {i}"
        early = _ev(fact, ["reliable.com"], [["art-1"]], extremity="ordinary")
        late = _ev(fact, ["reliable.com", "b.com", "c.com"],
                   [["art-1"], ["art-2"], ["art-3"]], extremity="ordinary")
        events.extend([early, late])
    recs = fold_reputation(events)
    by = reputation_by_source(recs)
    assert by["reliable.com"].confirmation_rate == 1.0
    assert by["reliable.com"].facts_confirmed == 3


def test_confirmation_rate_mixed():
    # 2 confirmed, 1 refuted → rate = 2/3
    events = []
    for i in range(2):
        fact = f"Good fact {i}"
        early = _ev(fact, ["mixed.com"], [["art-1"]], extremity="ordinary")
        late = _ev(fact, ["mixed.com", "b.com", "c.com"],
                   [["art-1"], ["art-2"], ["art-3"]], extremity="ordinary")
        events.extend([early, late])
    bad_fact = "False claim"
    bad_early = _ev(bad_fact, ["mixed.com"], [["art-1"]], extremity="notable")
    bad_late = _ev(bad_fact, ["mixed.com"], [["art-1"]], corrected=True, extremity="notable")
    events.extend([bad_early, bad_late])
    recs = fold_reputation(events)
    by = reputation_by_source(recs)
    assert by["mixed.com"].facts_confirmed == 2
    assert by["mixed.com"].facts_refuted == 1
    assert abs(by["mixed.com"].confirmation_rate - 2 / 3) < 0.01


# ---------------------------------------------------------------------------
# fold_reputation: solo_extraordinary red-flag
# ---------------------------------------------------------------------------

def test_solo_extraordinary_flagged():
    # Single source on an extraordinary claim → red flag
    ev = _ev(
        "World war imminent says blogger",
        ["crazytown.net"],
        [["art-1"]],
        extremity="extraordinary",
    )
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    assert by["crazytown.net"].solo_extraordinary == 1


def test_solo_ordinary_not_flagged():
    # Single source but ordinary claim → NOT a red flag
    ev = _ev(
        "Local council voted on budget",
        ["localgazette.com"],
        [["art-1"]],
        extremity="ordinary",
    )
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    assert by["localgazette.com"].solo_extraordinary == 0


def test_solo_extraordinary_significant_also_flagged():
    ev = _ev(
        "Entire sector collapses",
        ["singleoutlet.com"],
        [["art-1"]],
        extremity="significant",
    )
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    assert by["singleoutlet.com"].solo_extraordinary == 1


def test_solo_extraordinary_multi_source_not_flagged():
    # Two independent sources on an extraordinary claim → solo_extraordinary NOT raised
    ev = _ev(
        "Alien contact made",
        ["source-a.com", "source-b.com"],
        [["art-1"], ["art-2"]],
        extremity="extraordinary",
    )
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    assert by["source-a.com"].solo_extraordinary == 0
    assert by["source-b.com"].solo_extraordinary == 0


# ---------------------------------------------------------------------------
# fold_reputation: primary-source tracking
# ---------------------------------------------------------------------------

def test_primary_source_tracked():
    ev = _ev(
        "Interest rate raised",
        ["ecb.europa.eu", "ft.com"],
        [["art-1"], ["art-2"]],
        has_primary=True,
    )
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    assert by["ecb.europa.eu"].primary_appearances == 1
    assert by["ft.com"].primary_appearances == 0  # not a primary source


def test_primary_source_ministry():
    ev = _ev(
        "New policy announced",
        ["us.gov", "nytimes.com"],
        [["art-1"], ["art-2"]],
        has_primary=True,
    )
    recs = fold_reputation([ev])
    by = reputation_by_source(recs)
    assert by["us.gov"].primary_appearances == 1


# ---------------------------------------------------------------------------
# fold_reputation: sort order — more reliable sources rank first
# ---------------------------------------------------------------------------

def test_reliable_source_ranks_above_unreliable():
    # reliable.com: appears in a fact that confirms
    # shoddy.com: appears in a fact that gets refuted
    fact_good = "Merger confirmed"
    fact_bad = "CEO embezzled (retracted)"

    good_early = _ev(fact_good, ["reliable.com"], [["art-1"]], extremity="ordinary")
    good_late = _ev(fact_good, ["reliable.com", "b.com", "c.com"],
                    [["art-1"], ["art-2"], ["art-3"]], extremity="ordinary")

    bad_early = _ev(fact_bad, ["shoddy.com"], [["art-1"]], extremity="notable")
    bad_late = _ev(fact_bad, ["shoddy.com"], [["art-1"]], corrected=True, extremity="notable")

    recs = fold_reputation([good_early, good_late, bad_early, bad_late])
    names = [r.source for r in recs]
    assert names.index("reliable.com") < names.index("shoddy.com")


def test_source_with_outcomes_ranks_above_one_without():
    # Source A has confirmed facts; Source B has only unresolved facts.
    fact_a = "Treaty signed"
    early_a = _ev(fact_a, ["source-a.com"], [["art-1"]], extremity="ordinary")
    late_a = _ev(fact_a, ["source-a.com", "b.com", "c.com"],
                 [["art-1"], ["art-2"], ["art-3"]], extremity="ordinary")

    fact_b = "Talks ongoing"
    early_b = _ev(fact_b, ["source-b.com"], [["art-1"]], extremity="ordinary")
    # only 2 groups → corroborating, not terminal
    late_b = _ev(fact_b, ["source-b.com", "c.com"], [["art-1"], ["art-2"]], extremity="ordinary")

    recs = fold_reputation([early_a, late_a, early_b, late_b])
    names = [r.source for r in recs]
    assert names.index("source-a.com") < names.index("source-b.com")


def test_sort_is_deterministic():
    """Same inputs, different dict iteration order → same output order."""
    events = [
        _ev("Fact A", ["z.com", "a.com", "m.com"], [["art-1"], ["art-2"], ["art-3"]]),
        _ev("Fact B", ["z.com"], [["art-1"]], extremity="ordinary"),
    ]
    r1 = [r.source for r in fold_reputation(events)]
    r2 = [r.source for r in fold_reputation(events)]
    assert r1 == r2


# ---------------------------------------------------------------------------
# fold_reputation: multi-tick same fact (trajectory behaviour)
# ---------------------------------------------------------------------------

def test_fact_grouped_across_ticks():
    """Events for the same fact across multiple clock ticks are grouped — counted once per source."""
    fact = "Ceasefire agreed"
    tick1 = _ev(fact, ["reuters.com"], [["art-1"]], extremity="notable")
    tick2 = _ev(fact, ["reuters.com", "bbc.co.uk"], [["art-1"], ["art-2"]], extremity="notable")
    tick3 = _ev(fact, ["reuters.com", "bbc.co.uk", "apnews.com", "le-monde.fr"],
                [["art-1"], ["art-2"], ["art-3"], ["art-4"]], extremity="notable")

    recs = fold_reputation([tick1, tick2, tick3])
    by = reputation_by_source(recs)

    # Reuters should have exactly 1 appearance (one fact), not 3
    assert by["reuters.com"].appearances == 1
    # And it should be confirmed (4 independent originators ≥ confirm_at=3)
    assert by["reuters.com"].facts_confirmed == 1


def test_case_insensitive_fact_grouping():
    """Fact normalisation groups 'Minister X resigned' and 'minister x  resigned' as one fact."""
    fact_a = "Minister X resigned"
    fact_b = "minister x  resigned"  # different case + extra space
    tick1 = _ev(fact_a, ["reuters.com"], [["art-1"]], extremity="notable")
    tick2 = _ev(fact_b, ["reuters.com", "bbc.co.uk", "apnews.com"],
                [["art-1"], ["art-2"], ["art-3"]], extremity="notable")

    recs = fold_reputation([tick1, tick2])
    by = reputation_by_source(recs)
    # They are the same fact → reuters has 1 appearance, confirmed
    assert by["reuters.com"].appearances == 1
    assert by["reuters.com"].facts_confirmed == 1


# ---------------------------------------------------------------------------
# fold_reputation: independent_rate field
# ---------------------------------------------------------------------------

def test_independent_rate_zero_when_always_collapsed():
    # Source always appears in clusters where only one originator group forms
    events = []
    for i in range(3):
        ev = _ev(f"Wire story {i}", ["wire.com", "syndicator.com"], [["art-1", "art-2"]])
        events.append(ev)
    recs = fold_reputation(events)
    by = reputation_by_source(recs)
    assert by["wire.com"].independent_rate == 0.0
    assert by["syndicator.com"].independent_rate == 0.0


def test_independent_rate_one_when_always_independent():
    events = []
    for i in range(4):
        ev = _ev(f"Breaking fact {i}", ["independent.com", "other.com"],
                 [["art-1"], ["art-2"]])
        events.append(ev)
    recs = fold_reputation(events)
    by = reputation_by_source(recs)
    assert by["independent.com"].independent_rate == 1.0


# ---------------------------------------------------------------------------
# reputation_by_source: index helper
# ---------------------------------------------------------------------------

def test_reputation_by_source_lookup():
    events = [
        _ev("Fact X", ["a.com", "b.com"], [["art-1"], ["art-2"]]),
    ]
    recs = fold_reputation(events)
    idx = reputation_by_source(recs)
    assert set(idx.keys()) == {"a.com", "b.com"}
    assert isinstance(idx["a.com"], SourceReputation)


def test_reputation_by_source_empty():
    assert reputation_by_source([]) == {}


# ---------------------------------------------------------------------------
# Regression: multiple distinct facts with overlapping sources
# ---------------------------------------------------------------------------

def test_multiple_facts_overlapping_sources():
    """A source that appears in many confirmed facts should have appearances == n_facts."""
    events = []
    fact_names = ["Rate cut", "Bond yield rose", "Inflation fell", "GDP up"]
    for fact in fact_names:
        early = _ev(fact, ["bloomberg.com"], [["art-1"]], extremity="ordinary")
        late = _ev(fact, ["bloomberg.com", "ft.com", "wsj.com"],
                   [["art-1"], ["art-2"], ["art-3"]], extremity="ordinary")
        events.extend([early, late])

    recs = fold_reputation(events)
    by = reputation_by_source(recs)
    assert by["bloomberg.com"].appearances == len(fact_names)
    assert by["bloomberg.com"].facts_confirmed == len(fact_names)
    assert by["bloomberg.com"].confirmation_rate == 1.0


# ---------------------------------------------------------------------------
# Edge: corrected flag on any tick in the history → refuted
# ---------------------------------------------------------------------------

def test_correction_on_intermediate_tick_refutes():
    fact = "Prime minister arrested"
    tick1 = _ev(fact, ["tabloid.com"], [["art-1"]], extremity="extraordinary")
    tick2 = _ev(fact, ["tabloid.com"], [["art-1"]], corrected=True, extremity="extraordinary")
    tick3 = _ev(fact, ["tabloid.com"], [["art-1"]], extremity="extraordinary")  # follow-up

    recs = fold_reputation([tick1, tick2, tick3])
    by = reputation_by_source(recs)
    # The correction flag on tick2 should override — outcome = refuted
    assert by["tabloid.com"].facts_refuted == 1
    assert by["tabloid.com"].facts_confirmed == 0
