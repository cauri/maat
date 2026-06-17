"""Source ownership resolution + grouping (#41 / #254) — pure logic; the Wikidata client is mocked."""

from __future__ import annotations

from maat.pipeline.ownership import direct_owners, domain_of, fold_ownership, pick_entity


def test_domain_of():
    assert domain_of("https://www.reuters.com/world/x") == "reuters.com"
    assert domain_of("nypost.com") == "nypost.com"
    assert domain_of("") == ""


def test_pick_entity_prefers_official_site_domain_over_rank():
    candidates = [{"id": "Q1"}, {"id": "Q2"}]
    claims = {"Q1": {"P856": ["https://other.example"]}, "Q2": {"P856": ["https://www.reuters.com/"]}}
    assert pick_entity(candidates, "reuters.com", claims) == "Q2"  # domain match beats the top hit


def test_pick_entity_falls_back_to_top_hit_then_none():
    assert pick_entity([{"id": "Q1"}, {"id": "Q2"}], "", {}) == "Q1"
    assert pick_entity([], "x.example", {}) is None


def test_direct_owners_unions_parent_org_and_owned_by_deduped():
    assert direct_owners({"P749": ["Q10"], "P127": ["Q10", "Q20"]}) == ["Q10", "Q20"]
    assert direct_owners({}) == []


def test_fold_ownership_groups_outlets_sharing_a_controlling_owner():
    # New York Post and the WSJ share owner Q185278 (News Corporation) → one ownership group;
    # an outlet with a different owner stays independent (not in the map).
    resolved = [
        {"canonical": "new york post", "owners": [
            {"qid": "Q14289857", "label": "News Corp"}, {"qid": "Q185278", "label": "News Corporation"}]},
        {"canonical": "wsj", "owners": [
            {"qid": "Q185278", "label": "News Corporation"}, {"qid": "Q1126244", "label": "Dow Jones"}]},
        {"canonical": "the guardian", "owners": [{"qid": "Q42", "label": "Guardian Media Group"}]},
    ]
    groups = fold_ownership(resolved)
    assert groups["new york post"] == groups["wsj"]   # co-owned → collapse to one group
    assert "the guardian" not in groups               # alone → independent


def test_fold_ownership_label_is_the_shared_owner_and_empty_skipped():
    g = fold_ownership([
        {"canonical": "a", "owners": [{"qid": "Q1", "label": "Parent"}]},
        {"canonical": "b", "owners": [{"qid": "Q1", "label": "Parent"}]},
    ])
    assert g["a"] == g["b"] == "Parent"
    assert fold_ownership([{"canonical": "x", "owners": []}]) == {}  # no owner → no group


def test_agent_resolve_with_mocked_wikidata(monkeypatch):
    import maat.agents.ownership_agent as oa
    from maat.acquire import wikidata

    monkeypatch.setattr(wikidata, "search_entities", lambda name, limit=5: [{"id": "Q1", "label": "NYP"}])
    entities = {
        "Q1": {"label": "New York Post", "P856": ["https://nypost.com"], "P127": ["Q9"], "P749": []},
        "Q9": {"label": "News Corp"},
    }
    monkeypatch.setattr(wikidata, "entity_claims", lambda qid: entities[qid])
    info = oa._resolve("New York Post", "nypost.com")
    assert info["entity"] == "Q1" and info["entity_label"] == "New York Post"
    assert info["owners"] == [{"qid": "Q9", "label": "News Corp"}]
    assert info["provenance"] == "wikidata"
