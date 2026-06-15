"""Console tests (§5.7, P8) — story rollup, confidence derivation, audit render. No DB."""

import datetime as dt

from maat.pipeline.corroborate import confidence_read
from maat.web.app import (
    _audit_page,
    _claim_badges,
    _confidence_label,
    _group_stories,
    _nav,
    derivation_explain,
)


def test_confidence_label_tiers():
    assert _confidence_label(0.97) == ("Well corroborated", "hi")
    assert _confidence_label(0.75)[1] == "mid"
    assert _confidence_label(0.45)[1] == "lo"
    # the floor: a thin claim is flagged, not presented as established
    assert _confidence_label(0.32) == ("Thinly sourced", "floor")


def test_group_stories_splits_by_article_overlap_and_picks_headline():
    # two stories: resignation clusters share article a2; gold clusters share b1; no overlap
    clusters = [
        {"sources": ["S1"], "originators": [["a2"]], "confidence": 0.60},  # resignation secondary
        {"sources": ["S1", "S2", "S3"], "originators": [["a1", "a2", "a3"]], "confidence": 0.97},
        {"sources": ["X1", "X2", "X3"], "originators": [["b1", "b2", "b3"]], "confidence": 0.32},
        {"sources": ["X1"], "originators": [["b1"]], "confidence": 0.60},  # gold secondary
    ]
    stories = _group_stories(clusters)
    assert len(stories) == 2
    # the headline is the most-asserted claim (most sources), even when it's the LOW-confidence
    # one — the gold story leads with the 0.32 extraordinary claim, not its 0.60 footnote
    headlines = {round(s[0]["confidence"] * 100) for s in stories}
    assert headlines == {97, 32}


def test_derivation_explain_tracks_confidence_read():
    # F2: the operator-facing derivation must report the same number confidence_read computes
    s = derivation_explain(2, False, "notable")
    assert "2 independent originators" in s
    assert "prior: notable" in s
    assert f"{round(confidence_read(2, False, 'notable') * 100)}% confidence" in s
    assert "primary source" not in s
    # singular grammar + the primary-source lift called out
    s1 = derivation_explain(1, True, "extraordinary")
    assert "1 independent originator ·" in s1
    assert "primary source" in s1


def test_claim_badges_flag_corrections_and_laundering():
    c = {
        "in_headline": True, "voice": "attributed", "speaker": "Araghchi", "kind": "fact",
        "is_synthesis": False, "horizon": None, "corrected": True,
        "laundering_flag": "endorsement",
    }
    b = _claim_badges(c)
    assert "headline" in b and "quoted · Araghchi" in b and "fact" in b
    assert "you fixed this" in b and "flagged · endorsement" in b


def test_audit_page_strips_prefix_shows_reason_and_extras_and_handles_empty():
    assert "No changes yet" in _audit_page([])
    rows = [
        {
            "type": "admin.cluster.split",
            "data": {"target": "c1", "actor": "operator", "reason": "over-merged", "into": ["x", "y"]},
            "created_at": dt.datetime(2026, 6, 14, 22, 5),
        }
    ]
    out = _audit_page(rows)
    assert "split a story" in out  # plain-language action label
    assert "over-merged" in out
    assert "into=" in out  # non-standard fields surfaced


def test_nav_marks_active_tab():
    assert 'class="on"' in _nav("content")
    assert "History" in _nav("audit")
    assert "Quality" in _nav("content")  # renamed eval tab present


def test_eval_page_surfaces_pass_fail_and_metrics():
    # A4a renders the REAL eval harness output (no rebuild). evaluate() is pure over dicts.
    from maat.evals import evaluate
    from maat.web.app import _eval_page

    clusters = [
        {
            "fact": "Minister X resigned", "sources": ["AFP", "Daily"], "originators": [["a", "b"]],
            "independent_originators": 2, "has_primary": False, "confidence": 0.75,
            "extremity": "notable",
        }
    ]
    claims = [{"kind": "fact"}, {"kind": "projection"}]
    ok = evaluate(clusters, claims, {"resign": {"match": "resigned", "independent_originators": 2}})
    out = _eval_page(ok, "", "")
    assert "PASS" in out and "1/1 golden stories" in out
    assert "2 (want 2)" in out  # the golden check detail is surfaced verbatim

    bad = evaluate(clusters, claims, {"resign": {"match": "resigned", "independent_originators": 9}})
    assert "FAIL" in _eval_page(bad, "", "")


def test_eval_page_otlp_note_and_missing_fixtures():
    from maat.web.app import _eval_page

    assert "open cat-cafe" in _eval_page(None, "no fixtures", "")  # cat-cafe always surfaced
    assert "not receiving yet" in _eval_page(None, "no fixtures", "")  # status when OTLP unset
    assert "no fixtures" in _eval_page(None, "no fixtures", "")
    assert "receiving traces" in _eval_page(None, "x", "http://localhost:4318")


def test_stage_summary_maps_event_types_to_stages():
    from maat.web.app import stage_summary

    rows = stage_summary(
        {"claims.extracted": {"n": 5, "last": None}, "cluster.corroborated": {"n": 2, "last": None}}
    )
    by = {r["label"]: r for r in rows}
    assert by["Pull out claims"]["count"] == 5
    assert by["Score corroboration"]["count"] == 2
    assert by["Label claims"]["count"] == 0  # an absent event type reads as zero, not missing
    assert all("make" in r["cmd"] for r in rows)


def test_runs_page_shows_stages_dead_letters_and_recent():
    from maat.web.app import _runs_page, stage_summary

    stages = stage_summary({"article.ingested": {"n": 3, "last": dt.datetime(2026, 6, 15, 9, 0)}})
    proj = {"articles": 3, "claims": 0, "clusters": 0, "events": 3}
    recent = [{"type": "article.ingested", "stream_id": "a1", "created_at": dt.datetime(2026, 6, 15, 9, 0)}]
    dead = [
        {"type": "cluster.corroborated", "stream_id": "c1", "error": "boom",
         "created_at": dt.datetime(2026, 6, 15, 9, 1)}
    ]
    out = _runs_page(stages, proj, recent, dead)
    assert "Find articles" in out
    assert "Errors — failed and skipped" in out and "boom" in out
    assert "Recent activity" in out


def test_config_knobs_sourced_from_live_code():
    from maat.config import KNOBS_BY_KEY, groups
    from maat.providers.seam import CLAUDE_JUDGE

    assert {"model.judge", "gate.floor", "cluster.same_fact"} <= set(KNOBS_BY_KEY)
    assert KNOBS_BY_KEY["model.judge"]["core"] is True
    assert KNOBS_BY_KEY["model.bulk"]["core"] is False
    assert KNOBS_BY_KEY["model.judge"]["default"] == CLAUDE_JUDGE  # not invented
    assert "Model routing" in groups()


def test_config_page_shows_default_override_and_signoff_guard():
    from maat.web.app import _config_page

    out = _config_page(
        {"gate.floor": {"value": "0.35", "reason": "too strict", "at": dt.datetime(2026, 6, 15, 10, 0)}}
    )
    assert "Settings" in out
    assert "0.35" in out and "not applied yet" in out  # the suggestion is shown, marked not-live
    assert "suggestion" in out  # the propose-only guardrail is surfaced
    assert "needs sign-off" in out  # core knobs flagged


def test_wire_collapsed_sources_flags_only_multi_article_groups():
    from maat.web.app import wire_collapsed_sources

    id_to_source = {"a1": "AFP", "a2": "Daily News", "a3": "Indie Times"}
    clusters = [{"originators": [["a1", "a2"], ["a3"]]}]  # a1+a2 are one wire node; a3 independent
    assert wire_collapsed_sources(clusters, id_to_source) == {"AFP", "Daily News"}


def test_sources_page_registry_badges_and_proposal_note():
    from maat.web.app import _sources_page

    srcs = [
        {"source": "European Central Bank", "n": 3, "last": dt.datetime(2026, 6, 15), "langs": ["en"]},
        {"source": "AFP", "n": 9, "last": dt.datetime(2026, 6, 15), "langs": ["en", "fr"]},
    ]
    out = _sources_page(srcs, {"AFP"}, {"AFP": {"status": "deny", "reason": "wire"}}, {"AFP": "Wire"})
    assert "European Central Bank" in out and "first-hand" in out  # first-hand source role shown
    assert "reprint" in out and "denied" in out and "group · Wire" in out
    assert "preferences" in out  # saved-as-preference note surfaced


def test_nav_includes_all_p8_tabs():
    from maat.web.app import _nav

    n = _nav("content")
    for label in ("Feed", "Activity", "Updates", "Settings", "Prompts", "Sources", "Quality", "History"):
        assert label in n


def test_prompt_registry_and_placeholder_guard():
    from maat import prompts

    assert {"extract", "classify", "extremity"} <= set(prompts.PROMPTS_BY_KEY)
    assert prompts.seed_default("classify")  # non-empty in-code seed
    assert "{claim}" in prompts.missing_placeholders("extremity", "no placeholder here")  # caught
    assert prompts.missing_placeholders("extremity", "rate {claim}") == []  # intact -> ok


def test_prompt_registry_surfaces_all_runtime_prompts_with_status_and_source():
    """Every runtime prompt — backend (active + draft) and on-device — is registered with a
    status and a source so cauri can review them all in the console."""
    from maat import prompts

    # All eight prompts are present.
    assert set(prompts.PROMPTS_BY_KEY) == {
        "extract", "classify", "extremity",          # active backend
        "topics_enrich", "curation_geotag", "triage_llm",  # draft backend
        "summarizer_ondevice", "reranker_ondevice",  # on-device (Apple)
    }
    # Every entry carries a non-empty status, source, and prompt text.
    for p in prompts.PROMPTS:
        assert p["status"] in ("active", "draft", "on-device"), p["key"]
        assert p["source"], p["key"]
        assert p["default"].strip(), p["key"]
    # Status partitioning is exactly as intended.
    by_status = {}
    for p in prompts.PROMPTS:
        by_status.setdefault(p["status"], set()).add(p["key"])
    assert by_status["active"] == {"extract", "classify", "extremity"}
    assert by_status["draft"] == {"topics_enrich", "curation_geotag", "triage_llm"}
    assert by_status["on-device"] == {"summarizer_ondevice", "reranker_ondevice"}
    # Only the active prompts are editable; draft/on-device are read-only.
    assert prompts.EDITABLE_KEYS == frozenset({"extract", "classify", "extremity"})


def test_draft_prompts_imported_live_from_their_modules():
    """Draft prompt text is imported from the owning module so the console can never drift."""
    from maat import prompts
    from maat.agents.curation import _DRAFT_GEOTAG_PROMPT
    from maat.agents.triage import TRIAGE_LLM_PROMPT
    from maat.serving.topics import _LLM_PROMPT_TEMPLATE

    assert prompts.PROMPTS_BY_KEY["topics_enrich"]["default"] == _LLM_PROMPT_TEMPLATE
    assert prompts.PROMPTS_BY_KEY["curation_geotag"]["default"] == _DRAFT_GEOTAG_PROMPT
    assert prompts.PROMPTS_BY_KEY["triage_llm"]["default"] == TRIAGE_LLM_PROMPT
    # Draft prompts have no editable placeholders.
    assert prompts.missing_placeholders("triage_llm", "anything") == []


def test_ondevice_mirror_matches_swift_source_verbatim():
    """The on-device prompts are mirrored byte-for-byte from the Swift source they name.

    Guards against silent drift between the Apple Foundation Models prompts and the console
    mirror. Reads the `instructions:` and `prompt` blocks from the Swift files (dedented the way
    Swift presents a multi-line string literal) and asserts they appear verbatim in the mirror.
    """
    import pathlib

    from maat import prompts

    # Repo root: this test lives at <root>/python/tests/test_web.py
    root = pathlib.Path(__file__).resolve().parents[2]

    def swift_blocks(rel: str) -> list[str]:
        src = (root / rel).read_text()
        out, i = [], 0
        while True:
            start = src.find('"""', i)
            if start == -1:
                break
            end = src.find('"""', start + 3)
            lines = src[start + 3 : end].split("\n")
            indent = lines[-1]  # closing-delimiter indentation Swift strips from each line
            content = lines[1:-1]
            out.append(
                "\n".join(line[len(indent):] if line.startswith(indent) else line for line in content)
            )
            i = end + 3
        return out

    for key, rel in (
        ("summarizer_ondevice", "apple/Maat/Services/Summarizer.swift"),
        ("reranker_ondevice", "apple/Maat/Services/Reranker.swift"),
    ):
        entry = prompts.PROMPTS_BY_KEY[key]
        assert entry["source"] == rel  # registry pins the canonical source path
        # Locate the Swift file (tolerate a relocated Services/ -> Shared/ layout on side branches).
        candidates = [rel, rel.replace("/Services/", "/Shared/")]
        path = next((c for c in candidates if (root / c).exists()), None)
        assert path is not None, f"{key}: cannot find Swift source ({candidates})"
        instructions, prompt = swift_blocks(path)  # both blocks must exist
        mirror = entry["default"]
        assert instructions in mirror, f"{key}: instructions block drifted from {path}"
        assert prompt in mirror, f"{key}: prompt block drifted from {path}"


def test_active_text_falls_back_to_seed_without_pool():
    import asyncio

    from maat import prompts

    assert asyncio.run(prompts.active_text(None, "extract", "SEED")) == "SEED"


def test_prompts_page_shows_active_seed_history_and_rollback():
    from maat.web.app import _prompts_page

    base = _prompts_page({})  # no edits yet
    assert "Prompts" in base and "live on the next run" in base and "built-in" in base
    rows = {
        "extract": [
            {"key": "extract", "version": 2, "text": "v2 {article_text} {source_metadata} {detected_language}",
             "active": True, "reason": "tweak", "created_at": dt.datetime(2026, 6, 15, 9, 0)},
            {"key": "extract", "version": 1, "text": "v1", "active": False, "reason": "",
             "created_at": dt.datetime(2026, 6, 15, 8, 0)},
        ]
    }
    out = _prompts_page(rows)
    assert "version 2" in out and "Roll back" in out and "v1" in out


def test_prompts_page_groups_active_draft_and_ondevice_with_full_text():
    """The Prompts view surfaces every registered prompt, grouped by status, with each one's
    label, status, source, and full text. Active stays editable; draft + on-device are read-only.
    """
    import html as _html

    from maat import prompts
    from maat.web.app import _prompts_page

    out = _prompts_page({})

    # Group headings are present.
    assert "Active" in out
    assert "Draft — pending cauri review" in out
    assert "On-device (Apple)" in out

    # Every registered prompt shows up by label and source, with its full seed text.
    for p in prompts.PROMPTS:
        assert _html.escape(p["label"]) in out, p["key"]
        assert _html.escape(p["source"]) in out, p["key"]
        # A distinctive slice of each prompt's body is rendered (escaped).
        snippet = _html.escape(p["default"].strip().splitlines()[0])
        assert snippet in out, p["key"]

    # Draft + on-device blocks are read-only (a readonly textarea, no save form for them).
    assert "readonly" in out
    assert "Read-only — surfaced for review" in out

    # Editable backend prompts keep their save form + golden-test button.
    assert 'action="/prompts/save"' in out
    assert "Test on goldens" in out

    # A draft prompt's text is present but NOT inside a save form for that key.
    triage_snip = _html.escape(prompts.PROMPTS_BY_KEY["triage_llm"]["default"].strip().splitlines()[0])
    assert triage_snip in out
    # On-device entries are labelled as Foundation Models mirrors.
    assert "Foundation Models" in out


def test_is_paused_reads_latest_state_per_clock():
    from maat.clocks import is_paused

    newest_first = [{"clock": "ingestion", "paused": True}, {"clock": "ingestion", "paused": False}]
    assert is_paused(newest_first, "ingestion") is True  # latest wins
    assert is_paused([], "ingestion") is False  # never set -> running
    assert is_paused([{"clock": "harvester", "paused": True}], "ingestion") is False  # other clock


def test_read_topics_env_then_file(monkeypatch, tmp_path):
    from maat.clocks import read_topics

    monkeypatch.setenv("MAAT_TOPICS", "world politics, AI")
    assert read_topics(tmp_path) == ["world politics", "AI"]
    monkeypatch.delenv("MAAT_TOPICS", raising=False)
    assert read_topics(tmp_path) == []  # no env, no file
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "topics.txt").write_text("# comment\nAI safety\n\nelections\n")
    assert read_topics(tmp_path) == ["AI safety", "elections"]  # comments + blanks skipped


def test_clocks_page_status_topics_and_harvester_stub():
    from maat.web.app import _clocks_page

    running = _clocks_page({"n": 5, "last": dt.datetime(2026, 6, 15, 9, 0)}, [], ["AI"], False)
    assert "News updates" in running and "Pause updates" in running and "AI" in running
    assert "Prediction check" in running and "#39" in running  # harvester stub flagged
    paused = _clocks_page({"n": 5, "last": None}, [], [], True)
    assert "Paused" in paused and "Resume updates" in paused


def test_doc_renders_confirmation_banner_only_when_flashed():
    from maat.web.app import _doc

    with_flash = _doc("<p>x</p>", "", "content", flash="Saved.")
    assert 'class="flash"' in with_flash and "Saved." in with_flash
    assert 'class="flash"' not in _doc("<p>x</p>", "", "content")  # none when nothing happened


def test_redirect_carries_message_as_query():
    from maat.web.app import _redirect

    r = _redirect("/claim/abc", "Saved. Won't be overwritten.")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/claim/abc?ok=")
    assert _redirect("/sources").headers["location"] == "/sources"  # no message -> bare path


def test_runs_page_degrades_when_dead_letters_table_missing():
    from maat.web.app import _runs_page, stage_summary

    out = _runs_page(stage_summary({}), {"articles": 0}, [], [], dead_ready=False)
    assert "restart the kernel" in out  # graceful note, not a 500
    assert "Errors — failed and skipped" not in out  # no errors table rendered


def test_prompts_page_degrades_when_store_table_missing():
    from maat.web.app import _prompts_page

    out = _prompts_page({}, store_ready=False)
    assert "prompt store isn't set up yet" in out and "restart the kernel" in out
    assert "built-in" in out  # still shows the seed prompts
