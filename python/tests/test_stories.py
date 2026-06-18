"""Story roll-up assembly over the #42 story graph (#264): one headline + one score per story, with
a credibility trajectory on the detail view. Pure — clusters are plain dicts here."""

import datetime as dt

from maat.serving.stories import assemble_story, story_trajectory

PROVEN = {"reuters.com": 0.9, "apnews.com": 0.88, "bbc.com": 0.85}


def _cl(cid, fact, indep, *, sources, originators, conf=0.5, primary=False,
        extremity="notable", grounding=None, claim_ids=None):
    return {"id": cid, "fact": fact, "confidence": conf, "independent_originators": indep,
            "has_primary": primary, "extremity": extremity, "grounding": grounding,
            "sources": sources, "originators": originators, "claim_ids": claim_ids or []}


def _assemble(node_id, clusters, *, kind, id_to_source, reputation, text=None, pivots=None,
              disputed=None, hint="hint"):
    return assemble_story(
        node_id, hint, clusters, kind_by_claim=kind, text_by_claim=text or {}, pivots=pivots or {},
        id_to_source=id_to_source, reputation=reputation, disputed_claims=disputed or set(),
    )


def test_story_anchors_on_best_corroborated_fact():
    head = _cl("c1", "Big event happened.", 3, sources=["s1", "s2", "s3"],
               originators=[["a1"], ["a2"], ["a3"]], claim_ids=["cl1"])
    tangent = _cl("c2", "A minor detail.", 1, sources=["s4"], originators=[["a4"]], claim_ids=["cl2"])
    v = _assemble("node:1", [tangent, head],  # deliberately out of order
                  kind={"cl1": "fact", "cl2": "fact"},
                  id_to_source={"a1": "reuters.com", "a2": "apnews.com", "a3": "bbc.com", "a4": "blog.test"},
                  reputation=PROVEN)
    assert v.facts[0].cluster_id == "c1" and v.facts[0].is_headline   # anchored on the strong fact
    assert v.headline == "Big event happened."                       # headline == the anchor
    assert v.score.band == "corroborated" and v.score.score >= 70
    assert v.facts[0].sources[0].reputation is not None              # rated originator carries its rep
    assert v.cluster_count == 2 and not v.forecasts


def test_reputation_weighting_shows_through_the_layer():
    proven = _cl("c1", "X.", 3, sources=["s1", "s2", "s3"], originators=[["a1"], ["a2"], ["a3"]], claim_ids=["k1"])
    unknown = _cl("c1", "X.", 3, sources=["s1", "s2", "s3"], originators=[["b1"], ["b2"], ["b3"]], claim_ids=["k1"])
    kind = {"k1": "fact"}
    p = _assemble("n", [proven], kind=kind, reputation=PROVEN,
                  id_to_source={"a1": "reuters.com", "a2": "apnews.com", "a3": "bbc.com"})
    u = _assemble("n", [unknown], kind=kind, reputation=PROVEN,
                  id_to_source={"b1": "blog-a.test", "b2": "blog-b.test", "b3": "blog-c.test"})
    assert p.score.score > u.score.score and u.score.capped   # unproven carriers capped below proven


def test_forecast_only_story_splits_out_and_is_not_scored():
    fc = _cl("c1", "Markets will rise next year.", 0, sources=["s1"], originators=[["a1"]], claim_ids=["cl1"])
    v = _assemble("node:1", [fc], kind={"cl1": "projection"}, id_to_source={"a1": "x"}, reputation={}, hint=None)
    assert v.score.forecast_only and not v.facts and len(v.forecasts) == 1
    assert v.forecasts[0].is_projection and v.headline == "Markets will rise next year."


def test_wire_group_counts_once_and_is_flagged():
    cl = _cl("c1", "Event.", 1, sources=["s1", "s2"], originators=[["a1", "a2"]], claim_ids=["cl1"])
    v = _assemble("n", [cl], kind={"cl1": "fact"}, id_to_source={"a1": "x.com", "a2": "y.com"}, reputation={})
    assert len(v.facts[0].sources) == 1 and v.facts[0].sources[0].wire        # one originator group
    assert v.facts[0].independent_originators == 1


def test_non_english_fact_glosses_to_english_headline():
    cl = _cl("c1", "Hecho importante confirmado.", 2, sources=["s1", "s2"],
             originators=[["a1"], ["a2"]], claim_ids=["cl1"])
    v = _assemble("n", [cl], kind={"cl1": "fact"}, id_to_source={"a1": "x", "a2": "y"}, reputation={},
                  text={"cl1": "Hecho importante confirmado."}, pivots={"cl1": "Important event confirmed."})
    assert v.headline == "Important event confirmed."
    assert v.headline_orig == "Hecho importante confirmado." and v.facts[0].fact_en == "Important event confirmed."


def test_disputed_core_claim_flows_into_the_score():
    cl = _cl("c1", "Contested claim.", 3, sources=["s1", "s2", "s3"],
             originators=[["a1"], ["a2"], ["a3"]], claim_ids=["cl1"])
    v = _assemble("n", [cl], kind={"cl1": "fact"},
                  id_to_source={"a1": "reuters.com", "a2": "apnews.com", "a3": "bbc.com"},
                  reputation=PROVEN, disputed={"cl1"})
    assert v.score.band == "disputed" and v.facts[0].disputed


def test_trajectory_is_cumulative_and_excludes_projections():
    d1, d2 = dt.date(2026, 6, 15), dt.date(2026, 6, 16)

    def snap(cid, day, indep, conf, origs):
        return {"cluster_id": cid, "snapshot_day": day, "independent_originators": indep,
                "has_primary": False, "extremity": "notable", "confidence": conf,
                "originators": origs, "grounding": None, "corrected": False}

    snaps = [
        snap("c1", d1, 1, 0.4, [["a1"]]),
        snap("c1", d2, 3, 0.8, [["a1"], ["a2"], ["a3"]]),
        snap("cP", d2, 5, 0.9, [["a4"]]),   # a projection cluster — must not move the truth score
    ]
    pts = story_trajectory(snaps, projection_cluster_ids={"cP"}, reputation={}, id_to_source={})
    assert [p.day for p in pts] == ["2026-06-15", "2026-06-16"]
    assert pts[1].score > pts[0].score      # corroboration grew day-over-day → credibility rose


# --- console rendering + API serialisation (the same StoryView, two surfaces) ----------------

from maat.serving.stories import StorySource, StoryFact, StoryView, TrajectoryPoint  # noqa: E402
from maat.learning.story_credibility import FactView, score_story  # noqa: E402


def _view() -> StoryView:
    sc = score_story(
        [FactView(0.82, 3, True, "notable",
                  [["reuters.com"], ["apnews.com"], ["bbc.com"]], grounding="supported")],
        {"reuters.com": 0.9, "apnews.com": 0.88, "bbc.com": 0.85},
    )
    fact = StoryFact(
        cluster_id="c1", fact="A confirmed event.", fact_en=None, confidence=0.82,
        independent_originators=3, has_primary=True, extremity="notable", grounding="supported",
        disputed=False, is_headline=True, is_projection=False,
        sources=[StorySource(names=["reuters.com"], reputation=0.9, wire=False),
                 StorySource(names=["x.com", "y.com"], reputation=None, wire=True)],
    )
    return StoryView(
        node_id="node:abc", headline="A confirmed event.", headline_orig=None, score=sc,
        facts=[fact], forecasts=[], source_count=3, cluster_count=1, first_seen=1.0, last_updated=2.0,
        trajectory=[TrajectoryPoint("2026-06-15", 60, "developing"),
                    TrajectoryPoint("2026-06-16", 82, "corroborated")],
    )


def test_score_card_links_to_story_detail():
    from maat.web.app import _story_score_card

    html = _story_score_card(_view())
    assert 'href="/story/node:abc"' in html          # the stable story-graph node id, not a cluster
    assert "A confirmed event." in html and "%" in html


def test_detail_page_shows_derivation_trajectory_and_sourced_facts():
    from maat.web.app import _story_detail_page

    html = _story_detail_page(_view())
    assert "How this score was derived" in html        # the transparent breakdown
    assert "<polyline" in html and "2026-06-16" in html  # the trajectory sparkline
    assert "Core facts" in html and "/cluster/c1" in html
    assert "rep 90" in html and "wire · counted once" in html  # reputation + wire-collapse surfaced


def test_empty_trajectory_renders_a_friendly_note():
    from maat.web.app import _trajectory_svg

    assert "No history yet" in _trajectory_svg([])


def test_single_reading_trajectory_labels_itself_not_blank():
    from maat.web.app import _trajectory_svg

    svg = _trajectory_svg([TrajectoryPoint("2026-06-18", 81, "corroborated")])
    assert "first reading" in svg and "81%" in svg and "<polyline" not in svg


def test_story_to_json_round_trips_score_facts_and_trajectory():
    from maat.serving.feed import story_to_json

    payload = story_to_json(_view(), full=True)
    assert payload["id"] == "node:abc" and payload["band"] == "established"  # primary-backed, 3 reputable
    assert payload["facts"][0]["sources"][0]["reputation"] == 0.9
    assert payload["facts"][0]["sources"][1]["wire"] is True
    assert [p["score"] for p in payload["trajectory"]] == [60, 82]
    # the list form is lighter — no per-fact detail
    assert "facts" not in story_to_json(_view())
