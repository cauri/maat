"""#264 — story credibility roll-up. Locked model: headline-anchored, reputation-weighted
originators, neutral-but-capped cold-start, projections excluded, disputes penalised. Pure."""

from maat.learning.story_credibility import FactView, score_story


def _f(indep, rep_sources, *, conf=0.0, primary=False, extremity="notable", grounding=None, disputed=False):
    # one originator group per source name in rep_sources (each a distinct independent originator)
    groups = [[s] for s in rep_sources]
    return FactView(confidence=conf, independent_originators=indep, has_primary=primary,
                    extremity=extremity, originator_sources=groups, grounding=grounding, disputed=disputed)


PROVEN = {"reuters.com": 0.9, "apnews.com": 0.88, "bbc.com": 0.85}


def test_forecast_only_story_is_not_a_truth_score():
    s = score_story([], PROVEN)
    assert s.forecast_only and s.band == "forecast"


def test_well_corroborated_reputable_story_is_established():
    # 3 reputable independent originators reads "Corroborated" (~0.82); the TOP band needs more —
    # here a primary source backs it, pushing it over 0.85.
    head = _f(3, ["reuters.com", "apnews.com", "bbc.com"], primary=True, grounding="supported")
    s = score_story([head], PROVEN)
    assert s.band == "established" and s.score >= 85
    assert "3 with a track record" in s.why[0]


def test_three_reputable_originators_read_corroborated():
    s = score_story([_f(3, ["reuters.com", "apnews.com", "bbc.com"])], PROVEN)
    assert s.band == "corroborated" and 75 <= s.score < 85  # solid, not yet "strongly established"


def test_reputation_weighting_beats_same_count_of_unknowns():
    proven = score_story([_f(3, ["reuters.com", "apnews.com", "bbc.com"])], PROVEN)
    unknown = score_story([_f(3, ["blog-a.test", "blog-b.test", "blog-c.test"])], PROVEN)
    assert proven.score > unknown.score  # same 3 originators, but quality moves the number


def test_cold_start_only_is_neutral_but_capped():
    # 6 independent originators, all unrated → strong corroboration but capped below "established".
    s = score_story([_f(6, [f"new-outlet-{i}.test" for i in range(6)])], PROVEN)
    assert s.capped and s.score <= 70 and s.band != "established"
    assert any("not yet proven" in w for w in s.why)


def test_headline_anchored_tangents_do_not_drag_down():
    head = _f(3, ["reuters.com", "apnews.com", "bbc.com"])           # solid headline
    weak = _f(1, ["blog-a.test"], conf=0.3)                          # thin peripheral fact
    with_tangent = score_story([head, weak], PROVEN)
    headline_only = score_story([head], PROVEN)
    assert with_tangent.score >= headline_only.score  # the weak tangent never lowers it


def test_strong_supporting_facts_nudge_up():
    head = _f(2, ["reuters.com", "apnews.com"])
    strong_support = _f(2, ["bbc.com", "apnews.com"], conf=0.8)
    assert score_story([head, strong_support], PROVEN).score >= score_story([head], PROVEN).score


def test_single_source_is_unverified():
    s = score_story([_f(1, ["reuters.com"])], PROVEN)
    assert s.band in ("single", "thin")  # one originator, even reputable, isn't "established"


def test_disputed_core_claim_flags_and_penalises():
    head = _f(3, ["reuters.com", "apnews.com", "bbc.com"], disputed=True)
    s = score_story([head], PROVEN)
    assert s.band == "disputed"
    assert s.score < score_story([_f(3, ["reuters.com", "apnews.com", "bbc.com"])], PROVEN).score


def test_primary_grounding_lifts():
    plain = score_story([_f(2, ["reuters.com", "apnews.com"])], PROVEN)
    grounded = score_story([_f(2, ["reuters.com", "apnews.com"], primary=True, grounding="supported")], PROVEN)
    assert grounded.score > plain.score and any("primary" in w for w in grounded.why)
