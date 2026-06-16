"""Tests for the story-graph agent's concurrent LLM entity-spine extraction (#42).

The DRAFT LLM NER (one Sonnet call per cluster) is gated behind MAAT_STORY_GRAPH_LLM=1. Sequentially
that is ~7 min for the live cluster count and never completed within a tick — the rebuild silently
never emitted (0 story.graph.rebuilt events in prod). `_llm_entity_spines` runs the calls with bounded
concurrency and a per-call timeout + heuristic fallback so the rebuild always finishes.
"""

import asyncio

import maat.pipeline.story_graph_ner as ner
from maat.agents.story_graph_agent import _llm_entity_spines
from maat.pipeline.story_graph_build import entity_spine_heuristic


def test_llm_entity_spines_maps_each_text(monkeypatch):
    """Every input text gets its LLM spine; identical texts are deduped but still keyed."""
    monkeypatch.setattr(ner, "llm_entity_spine", lambda text, **_: ["ent:" + text])
    out = asyncio.run(_llm_entity_spines(["alpha", "beta", "alpha"], concurrency=4, timeout=5.0))
    assert out == {"alpha": ["ent:alpha"], "beta": ["ent:beta"]}


def test_llm_entity_spines_times_out_to_heuristic(monkeypatch):
    """A call that exceeds the per-call timeout falls back to the deterministic heuristic — one slow
    or hung Sonnet call can never stall the whole rebuild."""
    import time

    def fake(text, **_):
        if "slow" in text:
            time.sleep(1.0)  # exceeds the test timeout below → fallback path
            return ["LLM-should-not-win"]
        return ["ent:" + text]

    monkeypatch.setattr(ner, "llm_entity_spine", fake)
    out = asyncio.run(_llm_entity_spines(["fast", "slow item"], concurrency=4, timeout=0.15))
    assert out["fast"] == ["ent:fast"]
    # Timed out → heuristic, NOT the LLM's late answer.
    assert out["slow item"] == entity_spine_heuristic("slow item")
    assert out["slow item"] != ["LLM-should-not-win"]


def test_llm_entity_spines_falls_back_on_error(monkeypatch):
    """A provider error on a call also degrades to the heuristic rather than failing the rebuild."""
    def boom(text, **_):
        if text == "bad":
            raise RuntimeError("provider exploded")
        return ["ent:" + text]

    monkeypatch.setattr(ner, "llm_entity_spine", boom)
    out = asyncio.run(_llm_entity_spines(["good", "bad"], concurrency=2, timeout=5.0))
    assert out["good"] == ["ent:good"]
    assert out["bad"] == entity_spine_heuristic("bad")
