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

    # Every runtime + console prompt is present (incl. the prompt-chat helper's own draft, #159).
    assert set(prompts.PROMPTS_BY_KEY) == {
        "extract", "classify", "extremity", "acquire_queries",  # active backend
        "topics_enrich", "curation_geotag", "triage_llm",  # draft backend (gated)
        "prompt_chat_agent",                         # draft: the console chat helper's own prompt
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
    assert by_status["active"] == {"extract", "classify", "extremity", "acquire_queries"}
    assert by_status["draft"] == {"topics_enrich", "curation_geotag", "triage_llm", "prompt_chat_agent"}
    assert by_status["on-device"] == {"summarizer_ondevice", "reranker_ondevice"}
    # Only the active prompts are editable; draft/on-device are read-only.
    assert prompts.EDITABLE_KEYS == frozenset({"extract", "classify", "extremity", "acquire_queries"})


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
        ("summarizer_ondevice", "apple/Maat/Shared/Summarizer.swift"),
        ("reranker_ondevice", "apple/Maat/Shared/Reranker.swift"),
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


def test_every_prompt_has_a_description():
    # cauri reviews these in the console — each must say what it is for and how it is used
    from maat import prompts

    for p in prompts.PROMPTS:
        assert p.get("description"), f"{p['key']} is missing a description"
        assert len(p["description"]) > 30  # a real sentence, not a stub


def test_prompts_page_renders_descriptions():
    import html

    from maat import prompts
    from maat.web.app import _prompts_page

    out = _prompts_page({}, store_ready=True)
    for p in prompts.PROMPTS:
        assert html.escape(p["description"]) in out  # every prompt's purpose shows on the page


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


# ============================ P8 console tie-ins (#74/#76/#77/#78/#123) ======================
#
# Each new view is a fetch→pure-builder→HTML triple; we test the pure builders over the SAME
# event/record shapes the real backends emit. No DB — exactly like the views above.


def _corr_ev(
    fact, sources, originators, *, has_primary=False, extremity="notable",
    confidence=0.5, corrected=False,
):
    """A `cluster.corroborated` event dict, matching the corroborate agent's output."""
    return {
        "fact": fact, "sources": sources, "originators": originators,
        "independent_originators": len(originators), "has_primary": has_primary,
        "extremity": extremity, "confidence": confidence, "corrected": corrected,
    }


# A two-event history where one fact CONFIRMS (1→4 independent originators) and an extraordinary
# rumour stalls solo — enough to resolve outcomes and exercise the calibration/RL replay.
_CONFIRMING_HISTORY = [
    _corr_ev("Minister X resigned", ["reuters", "bbc"], [["a1"], ["a2"]], confidence=0.45),
    _corr_ev("Minister X resigned", ["reuters", "bbc", "afp", "dpa"],
             [["a1"], ["a2"], ["a3"], ["a4"]], confidence=0.9),
    _corr_ev("Aliens landed", ["tabloid"], [["b1"]], extremity="extraordinary", confidence=0.2),
]


def test_reputation_page_renders_fold_over_corroboration_history():
    # #74 — the REAL reputation fold (not the /api/sources proxy): trajectory, not a snapshot.
    from maat.learning.reputation import fold_reputation
    from maat.web.app import _reputation_page

    reps = fold_reputation(_CONFIRMING_HISTORY)
    out = _reputation_page(reps, len(_CONFIRMING_HISTORY))
    assert "Reputation" in out and "over time" in out
    assert "independent originator" in out  # the independence dimension is surfaced
    assert "confirmed" in out  # the truth-over-time outcome column
    assert "never" in out and "consensus" in out  # the anti-consensus framing is explicit
    # the lone extraordinary claimant is flagged, not silently scored
    assert "solo extraordinary" in out


def test_reputation_page_empty_history():
    from maat.web.app import _reputation_page

    out = _reputation_page([], 0)
    assert "No reputation yet" in out and "Reputation" in out


def test_reputation_tier_cold_start_is_unrated_not_zero():
    # §6.6 — a source with no resolved outcomes is "not yet rated", never scored on consensus.
    from maat.learning.reputation import SourceReputation
    from maat.web.app import _reputation_tier

    cold = SourceReputation(
        source="New Outlet", appearances=2, independent_appearances=2, independent_rate=1.0,
        primary_appearances=0, mean_attribution_weight=1.0, solo_extraordinary=0,
        facts_confirmed=0, facts_refuted=0, facts_unresolved=2, outcome_n=0,
        confirmation_rate=None, _reliability_rank=-1.0,
    )
    assert _reputation_tier(cold) == ("not yet rated", "own")
    hot = SourceReputation(
        source="Reliable", appearances=10, independent_appearances=9, independent_rate=0.9,
        primary_appearances=3, mean_attribution_weight=1.0, solo_extraordinary=0,
        facts_confirmed=9, facts_refuted=1, facts_unresolved=0, outcome_n=10,
        confirmation_rate=0.9, _reliability_rank=0.9,
    )
    assert _reputation_tier(hot)[0] == "highly reliable"


def test_de_us_breakdown_from_article_rows():
    # #76/#59 — country guessed from the source domain TLD; language straight off the row.
    from maat.web.app import de_us_breakdown

    arts = [
        {"source": "bbc.co.uk", "language": "en"},
        {"source": "lemonde.fr", "language": "fr"},
        {"source": "spiegel.de", "language": "de"},
    ]
    bd, geo, lang = de_us_breakdown(arts)
    assert 0.0 <= bd.overall <= 1.0
    assert "GB" in geo and "FR" in geo  # TLD→country resolved
    assert "fr" in lang and "en" in lang


def test_calibration_page_surfaces_brier_de_us_and_health():
    # #76 — three dashboards over the live backends (references, not rebuilds).
    import datetime as dt

    from maat.learning.calibration_prod import production_calibration
    from maat.obs_metrics import pipeline_health
    from maat.web.app import _calibration_page, de_us_breakdown

    now = dt.datetime(2026, 6, 15, 12, 0, tzinfo=dt.timezone.utc)
    status = production_calibration(_CONFIRMING_HISTORY, now=now)
    bd, geo, lang = de_us_breakdown([{"source": "bbc.co.uk", "language": "en"}])
    health = pipeline_health(
        [{"type": "article.ingested", "created_at": now}], [], {"articles": 1, "claims": 0, "clusters": 0}
    )
    out = _calibration_page(status, bd, geo, lang, health)
    assert "Brier" in out  # calibration metric (#60)
    assert "De-US-centering" in out and "Anglo share" in out  # de-US dashboard (#59)
    assert "Pipeline health" in out and "Alerts" in out  # observability (#61)


def test_calibration_page_handles_no_resolved_facts():
    import datetime as dt

    from maat.learning.calibration_prod import production_calibration
    from maat.obs_metrics import pipeline_health
    from maat.web.app import _calibration_page, de_us_breakdown

    status = production_calibration([], now=dt.datetime(2026, 6, 15, tzinfo=dt.timezone.utc))
    bd, geo, lang = de_us_breakdown([])
    health = pipeline_health([], [], {"articles": 0, "claims": 0, "clusters": 0})
    out = _calibration_page(status, bd, geo, lang, health)
    assert "Nothing has resolved yet" in out  # honest empty state, not a crash
    assert "empty" in out  # health status badge for an empty pipeline


def test_review_page_separates_routes_and_classifies_fresh_items():
    # #77 — review-routed needs a human; auto-fix is safe-to-PR; untriaged items classified live.
    import datetime as dt

    from maat.web.app import _review_page, _triage_preview, coordinated_signal

    review = [{
        "text": "the confidence score is wrong", "source": "reader",
        "triage": {"item_id": "f1", "category": "veracity-dispute", "route": "review",
                   "confidence": 0.85, "reason": "matched", "auto_fixable": False},
        "triaged_at": dt.datetime(2026, 6, 15, 9, 0),
    }]
    autofix = [{
        "text": "blank page on load", "source": "reader",
        "triage": {"item_id": "f2", "category": "bug", "route": "auto-fix",
                   "confidence": 0.82, "reason": "matched", "auto_fixable": True},
        "triaged_at": dt.datetime(2026, 6, 15, 9, 1),
    }]
    fresh = [_triage_preview({"item_id": "f3", "text": "the layout overlaps on mobile",
                              "source": "reader", "submitted_at": dt.datetime(2026, 6, 15, 9, 2)})]
    out = _review_page(review, autofix, fresh, coordinated_signal([]))
    assert "Needs review" in out and "Safe to auto-fix" in out
    assert "veracity dispute" in out  # category label rendered
    assert "not yet triaged" in out  # the live-classified item is flagged as such
    assert "untrusted" in out.lower()  # the attack-vector framing is present


def test_coordinated_signal_flags_bursts_from_one_source():
    # Feedback is untrusted input — a burst from one source is a candidate attack vector.
    from maat.web.app import coordinated_signal

    quiet = coordinated_signal([{"source": "a"}, {"source": "b"}, {"source": "a"}])
    assert quiet["suspicious"] == {}  # below threshold → nothing flagged
    loud = coordinated_signal([{"source": "botnet"}] * 6 + [{"source": "real"}])
    assert loud["suspicious"] == {"botnet": 6}  # the burst source is surfaced
    assert loud["total"] == 7


def test_review_page_warns_on_coordinated_feedback():
    from maat.web.app import _review_page, coordinated_signal

    coord = coordinated_signal([{"source": "botnet"}] * 6)
    out = _review_page([], [], [], coord)
    assert "coordinated" in out.lower() and "botnet" in out


def test_policy_page_shows_bounded_signoff_gated_proposal_and_grants():
    # #78 — the RL proposal is ALWAYS unapproved; capability grants state what may auto-tune.
    from maat.learning.rl import policy_step
    from maat.web.app import _policy_page

    proposal = policy_step(_CONFIRMING_HISTORY)
    assert proposal.approved is False  # the contract: never auto-applied
    out = _policy_page(proposal, len(_CONFIRMING_HISTORY))
    assert "Policy" in out and "needs sign-off" in out
    assert "A/B-on-replay" in out  # the weight side is justified by replay
    assert "Capability grants" in out and "bounded self-modification" in out
    assert "operator-gated" in out and "auto-tunable" in out  # both grant kinds shown
    # scoring authority and source standing are operator-gated (cannot self-escalate, §5)
    assert "Scoring thresholds" in out and "Source allow / deny" in out


def test_policy_page_empty_history_is_a_noop_proposal():
    from maat.learning.rl import policy_step
    from maat.web.app import _policy_page

    proposal = policy_step([])
    assert proposal.approved is False
    out = _policy_page(proposal, 0)
    assert "No facts have resolved yet" in out  # nothing to replay → policy == current


def test_weights_with_override_builds_candidate_or_none():
    # #123 — only weight knobs are replayable; bad keys/values return None (skipped, not crash).
    from maat.learning.calibration import Weights
    from maat.web.app import _weights_with_override

    base = Weights.defaults()
    w = _weights_with_override("decay.notable", "0.7")
    assert w is not None and w.decay["notable"] == 0.7
    assert w.decay["routine"] == base.decay["routine"]  # only the one level moved
    assert _weights_with_override("confidence.primary_lift", "0.6").primary_lift == 0.6
    assert _weights_with_override("gate.floor", "0.3") is None  # not a weight knob
    assert _weights_with_override("decay.notable", "not-a-number") is None  # unparseable
    assert _weights_with_override("decay.nonexistent", "0.5") is None  # unknown level


def test_config_page_shows_ab_replay_impact_and_revert():
    # #123 — at sign-off the Config panel shows Brier before/after + N facts changing verdict,
    # plus a revert control and per-knob change history.
    import datetime as dt

    from maat.learning.calibration import Weights, observations_from_history, replay_ab
    from maat.web.app import _config_page, _weights_with_override

    obs = observations_from_history(_CONFIRMING_HISTORY)
    cand = _weights_with_override("decay.notable", "0.8")
    ab = replay_ab(obs, base=Weights.defaults(), candidate=cand)
    overrides = {"decay.notable": {"value": "0.8", "reason": "tune", "at": dt.datetime(2026, 6, 15, 10, 0)}}
    history = {"decay.notable": [
        {"value": "0.8", "actor": "operator", "reverted": False, "at": dt.datetime(2026, 6, 15, 10, 0)}
    ]}
    out = _config_page(overrides, {"decay.notable": ab}, history)
    assert "A/B-on-replay" in out  # the impact is surfaced before sign-off
    assert "change verdict" in out  # promoted/demoted breakdown
    assert "Revert to default" in out  # the revert control
    assert "change history" in out  # per-knob history backs the revert


def test_config_page_backward_compatible_single_arg():
    # The original call site (and the older test) pass only overrides — must still render.
    import datetime as dt

    from maat.web.app import _config_page

    out = _config_page(
        {"gate.floor": {"value": "0.35", "reason": "too strict", "at": dt.datetime(2026, 6, 15, 10, 0)}}
    )
    assert "Settings" in out and "0.35" in out and "needs sign-off" in out
    assert "Revert to default" in out  # the revert control shows even without replay/history


def test_replay_block_handles_no_resolved_facts_and_none():
    from maat.learning.calibration import ReplayAB
    from maat.web.app import _replay_block

    assert _replay_block(None) == ""  # no proposal → nothing
    empty = ReplayAB(brier_base=None, brier_candidate=None, n_scored=0, flips=0, promoted=0, demoted=0)
    assert "no resolved facts" in _replay_block(empty)
    scored = ReplayAB(brier_base=0.3, brier_candidate=0.2, n_scored=5, flips=2, promoted=2, demoted=0)
    block = _replay_block(scored)
    assert "resolved facts" in block and "0.3→0.2" in block
    assert "2 promoted" in block and "better-calibrated" in block


def test_nav_includes_new_p8_dashboard_tabs():
    from maat.web.app import _nav

    n = _nav("reputation")
    for label in ("Review", "Policy", "Reputation", "Calibration"):
        assert label in n
    assert 'class="on"' in n  # the active tab is marked


def _all_route_paths(routes) -> set:
    """Collect every route path, descending into included sub-routers (FastAPI lazily wraps an
    `include_router` call, exposing the real routes under `original_router`/`router`)."""
    out: set = set()
    for r in routes:
        p = getattr(r, "path", None)
        if p:
            out.add(p)
        for attr in ("original_router", "router"):
            sub = getattr(r, attr, None)
            if sub is not None and hasattr(sub, "routes"):
                out |= _all_route_paths(sub.routes)
    return out


def test_feed_router_is_mounted_on_app():
    # The served-feed APIRouter (serving/feed.py) must be mounted so the Apple client gets data.
    from maat.web.app import app, feed_router

    assert feed_router is not None  # the router built (FastAPI available)
    paths = _all_route_paths(app.routes)
    assert "/api/v2/feed" in paths and "/api/v2/story/{cluster_id}" in paths


# ---------------------------------------------------------------------------
# Prompt-chat agent (#158/#159): the raw-Claude "Improve with chat" helper on editable prompts.
# ---------------------------------------------------------------------------

def _chat_post(body: dict):
    """POST JSON to /prompts/chat through the real ASGI app; return (status, json)."""
    import asyncio

    import httpx

    from maat.web.app import app

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/prompts/chat", json=body)
            return r.status_code, r.json()

    return asyncio.run(go())


def test_prompt_chat_returns_reply_with_claude_monkeypatched(monkeypatch):
    """Happy path: /prompts/chat formats the conversation, calls claude_complete, returns its text."""
    from maat.providers import seam

    seen = {}

    def fake_complete(prompt, *, model="m", max_tokens=256):
        seen["prompt"] = prompt
        return seam.Reply(text="Try: rate {claim} on a 1-5 scale.\n```\nrate {claim}\n```", model=model)

    monkeypatch.setattr(seam, "claude_complete", fake_complete)
    status, data = _chat_post(
        {"key": "extremity", "current": "rate {claim}",
         "messages": [{"role": "user", "content": "make it stricter"}]}
    )
    assert status == 200
    assert "reply" in data and "rate {claim}" in data["reply"]
    # The agent saw the prompt under discussion, its current text, and the conversation.
    assert "Extraordinary-claim rater" in seen["prompt"]  # the label
    assert "rate {claim}" in seen["prompt"]                # the current editor text, verbatim
    assert "make it stricter" in seen["prompt"]            # the running conversation


def test_prompt_chat_graceful_when_no_api_key(monkeypatch):
    """No ANTHROPIC_API_KEY -> claude_complete raises KeyError; the route returns a clear
    'unavailable' message at HTTP 200, never a 500 (the box runs without the reader's key)."""
    from maat.providers import seam

    def no_key(prompt, *, model="m", max_tokens=256):
        raise KeyError("ANTHROPIC_API_KEY")  # exactly what os.environ[...] raises when unset

    monkeypatch.setattr(seam, "claude_complete", no_key)
    status, data = _chat_post({"key": "extremity", "current": "rate {claim}", "messages": []})
    assert status == 200  # graceful, not a crash
    assert "reply" not in data
    assert "unavailable" in data["error"].lower()
    assert "ANTHROPIC_API_KEY" in data["error"]


def test_prompt_chat_graceful_when_provider_errors(monkeypatch):
    """Any provider/network error is caught: HTTP 200 with an error field, page keeps working."""
    from maat.providers import seam

    def boom(prompt, *, model="m", max_tokens=256):
        raise RuntimeError("upstream 529 overloaded")

    monkeypatch.setattr(seam, "claude_complete", boom)
    status, data = _chat_post({"key": "classify", "current": "{article_text} {claims_json}",
                               "messages": []})
    assert status == 200
    assert "unavailable" in data["error"].lower() and "529" in data["error"]


def test_prompt_chat_rejects_non_editable_keys(monkeypatch):
    """Draft / on-device prompts are read-only — chat refuses them and never calls the model."""
    from maat.providers import seam

    def must_not_run(prompt, *, model="m", max_tokens=256):  # pragma: no cover
        raise AssertionError("claude_complete must not be called for a read-only key")

    monkeypatch.setattr(seam, "claude_complete", must_not_run)
    for key in ("triage_llm", "summarizer_ondevice", "prompt_chat_agent", "nonexistent"):
        status, data = _chat_post({"key": key, "current": "x", "messages": []})
        assert status == 200, key
        assert "error" in data and "editable" in data["error"].lower(), key


def test_prompt_chat_builder_preserves_placeholders_verbatim():
    """The chat prompt inserts {placeholders} via replace, not str.format — so a current prompt
    full of {claim}/{article_text} tokens survives intact (a format() would KeyError/mangle them)."""
    from maat.web.app import PromptChatMsg, _prompt_chat_prompt

    current = "Extract from {article_text} using {source_metadata} in {detected_language}."
    built = _prompt_chat_prompt(
        "Claim extraction", "Pulls atomic claims.", current,
        [PromptChatMsg(role="user", content="be more precise"),
         PromptChatMsg(role="assistant", content="ok")],
    )
    assert "{article_text}" in built and "{source_metadata}" in built and "{detected_language}" in built
    assert "Claim extraction" in built and "be more precise" in built
    # The agent's own template tokens are all consumed (none leak into the final prompt).
    for tok in ("{prompt_label}", "{prompt_purpose}", "{current_prompt}"):
        assert tok not in built


def test_prompts_page_renders_chat_panel_on_editable_prompts_only():
    """Each editable prompt gets an 'Improve with chat' panel wired to its own textarea; the
    read-only draft/on-device prompts do not. The shared chat JS is present once."""
    from maat.web.app import _doc, _prompts_page

    page = _doc(_prompts_page({}), "prompts", "prompts")
    assert "Improve with chat" in page
    assert "maatPromptChat" in page  # the inline handler shipped
    for key in ("extract", "classify", "extremity"):  # editable -> panel + targeted textarea id
        assert f'id="ta-{key}"' in page, key
        assert f"maatPromptChat('{key}')" in page, key
        assert f'id="log-{key}"' in page and f'id="in-{key}"' in page, key
    # No chat panel/textarea is wired for a read-only prompt.
    assert 'id="ta-triage_llm"' not in page
    assert "maatPromptChat('triage_llm')" not in page


def test_prompt_chat_agent_prompt_in_registry_as_reviewable_draft():
    """#159: the chat helper's own instructions are surfaced in the registry as a draft with a
    description, so cauri can review them — and they are not part of the editable/live set."""
    from maat import prompts

    entry = prompts.PROMPTS_BY_KEY["prompt_chat_agent"]
    assert entry["status"] == "draft"
    assert entry["source"]
    assert len(entry["description"]) > 30
    assert entry["default"].strip()  # the actual instructions are surfaced for review
    assert "prompt_chat_agent" not in prompts.EDITABLE_KEYS  # never a live/editable override
    # The instructions keep the substitution points the console fills at chat time.
    for tok in ("{prompt_label}", "{prompt_purpose}", "{current_prompt}"):
        assert tok in prompts.PROMPT_CHAT_AGENT
