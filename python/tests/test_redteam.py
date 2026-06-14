"""Red-team the laundering / corroboration guards (#62, P7).

Adversarial cases that try to manufacture apparent corroboration. The guards must hold:
spread is not corroboration, and weakly-sourced spread must not read as well-established.
A couple of cases document KNOWN GAPS in the heuristic stand-ins, flagged for tuning.
"""

from maat.pipeline.corroborate import (
    attribution_weight,
    collapse_originators,
    confidence_read,
    effective_originators,
    has_provenance,
)


def test_redteam_wire_reprints_collapse_to_one_originator():
    # 10 outlets reprint ONE wire story verbatim — spread, not independent corroboration.
    body = "Minister X resigned on Tuesday amid a procurement scandal, the agency reported."
    ids = [f"outlet{i}" for i in range(10)]
    bodies = {i: body for i in ids}
    sources = {i: f"Outlet {i}" for i in ids}
    assert len(collapse_originators(ids, bodies, sources)) == 1


def test_redteam_citation_cascade_collapses_to_origin():
    # A chain that all cites the same origin must collapse to that one originator.
    bodies = {
        "afp": "A bridge collapsed in the capital, killing dozens.",
        "b": "A bridge collapsed in the capital, according to AFP.",
        "c": "Dozens died in a bridge collapse, as reported by AFP.",
    }
    sources = {"afp": "AFP", "b": "Daily B", "c": "Daily C"}
    assert len(collapse_originators(["afp", "b", "c"], bodies, sources)) == 1


def test_redteam_bald_spread_does_not_reach_well_corroborated():
    # 6 blogs assert an extraordinary claim with NO attribution — laundering by volume.
    ids = [f"blog{i}" for i in range(6)]
    bodies = {i: "A senior politician took a massive secret bribe." for i in ids}
    sources = {i: f"Blog {i}" for i in ids}
    eff = effective_originators([[i] for i in ids], bodies, sources)
    assert eff == round(6 * 0.3, 2)  # each bald originator -> 0.3
    # must NOT clear the "Well corroborated" bar despite 6-way spread
    assert confidence_read(eff, False, "extraordinary") < 0.85


def test_redteam_anonymous_only_is_capped_below_named():
    anon = attribution_weight("Officials, speaking on condition of anonymity, said the deal failed.", "Daily")
    named = attribution_weight("The Finance Ministry said the deal failed.", "Daily")
    assert anon < named
    assert anon == 0.6


def test_redteam_same_outlet_cannot_self_corroborate():
    # One outlet publishing the same claim across three articles is ONE originator.
    bodies = {f"a{i}": f"The bank acted, our desk reported (take {i})." for i in range(3)}
    sources = dict.fromkeys(bodies, "econotimes.com")
    assert len(collapse_originators(list(bodies), bodies, sources)) == 1


def test_redteam_known_gap_vague_marker_is_lenient():
    # KNOWN GAP (documented, not a pass/fail of the product): the provenance heuristic is
    # lenient — a vague "reportedly" with no named source still counts as attribution, because
    # we err toward not discounting real reporting. Flagged for tuning with cauri.
    assert has_provenance("The minister reportedly resigned.")  # "reported" substring


def test_redteam_known_gap_bald_volume_accumulates_linearly():
    # KNOWN GAP: bald originators accumulate linearly (n * 0.3), so enough bald spread still
    # creeps up. A cap or diminishing returns on weak sourcing is a tuning decision for cauri.
    many_bald = [[f"b{i}"] for i in range(12)]
    bodies = {f"b{i}": "It happened." for i in range(12)}
    sources = {f"b{i}": f"Blog {i}" for i in range(12)}
    assert effective_originators(many_bald, bodies, sources) == round(12 * 0.3, 2)  # = 3.6, not capped
