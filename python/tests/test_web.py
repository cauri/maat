"""Console tests (§5.7, P8) — story rollup, confidence derivation, audit render. No DB."""

import datetime as dt
import json

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
        {"source": "European Central Bank", "n": 3, "first": dt.datetime(2026, 6, 1),
         "last": dt.datetime(2026, 6, 15), "langs": ["en"]},
        {"source": "AFP", "n": 9, "first": dt.datetime(2026, 6, 2),
         "last": dt.datetime(2026, 6, 15), "langs": ["en", "fr"]},
        {"source": "lenta.ru", "n": 5, "first": dt.datetime(2026, 6, 3),
         "last": dt.datetime(2026, 6, 15), "langs": ["ru"]},
    ]
    from maat.learning.source_registry import fold_sources

    registry = fold_sources([
        {"source": "AFP", "state": "active", "reputation": 0.71, "at": "t"},
        {"source": "European Central Bank", "state": "registered", "at": "t"},
    ])
    out = _sources_page(
        srcs, {"AFP"}, {"AFP": {"status": "deny", "reason": "wire"}},
        {"AFP": {"label": "Agence France-Presse", "auto": True}},  # owner_by
        registry,
        stories_by={"AFP": 6}, align_by={"lenta.ru": "state"},
    )
    assert "European Central Bank" in out and "first-hand" in out  # first-hand source role shown
    assert "reprint" in out and "denied" in out
    assert "owner · Agence France-Presse" in out  # owner group (auto-resolved) shown
    assert "auto-resolved from Wikidata" in out  # tooltip distinguishes auto vs manual
    assert 'name="deny"' in out  # the deny toggle (one on/off control, no save button)
    assert "corroboration" in out  # the enforcement note surfaced
    # #241 lifecycle badges by their unique tooltips
    assert "In the live feed" in out and "Held out of the feed" in out
    # reputation is rendered BIG + bold (its own styled cell), not buried in the meta line
    assert 'class="srep' in out and ">0.71<" in out
    # new fields: country (ccTLD), corroboration count, state-media flag
    assert "Russia" in out and "6 in feed" in out and "state-affiliated" in out
    # styled hover tooltips (data-tip), not bare browser title=
    assert "data-tip=" in out
    # filter / sort / group controls + per-row data attributes for the client-side view
    assert 'id="src-q"' in out and 'id="src-sort"' in out and 'id="src-group"' in out
    assert 'class="src-tag" data-tag="state"' in out          # a tag-filter pill
    assert 'data-tags=' in out and 'data-rep=' in out and 'data-country=' in out
    assert 'id="src-list"' in out                              # container the JS sorts/groups
    # lenta.ru carries its state tag + ru→Russia country so it's filterable + groupable
    assert 'data-tags="state"' in out and 'data-country="Russia"' in out


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
        "extract", "classify", "extremity", "acquire_queries", "source_gate",  # active backend
        "topics_enrich", "curation_geotag", "triage_llm", "grounding",  # draft backend (gated)
        "prompt_chat_agent", "console_assistant",    # draft: console chat helper + page assistant
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
    assert by_status["active"] == {"extract", "classify", "extremity", "acquire_queries", "source_gate"}
    assert by_status["draft"] == {
        "topics_enrich", "curation_geotag", "triage_llm", "grounding",
        "prompt_chat_agent", "console_assistant"
    }
    assert by_status["on-device"] == {"summarizer_ondevice", "reranker_ondevice"}
    # #189: every backend prompt is editable — drafts are live like any other, just tagged for
    # review. Only on-device (Swift mirrors) stay read-only / out of the editable set.
    assert prompts.EDITABLE_KEYS == frozenset(
        {"extract", "classify", "extremity", "acquire_queries", "source_gate",
         "topics_enrich", "curation_geotag", "triage_llm", "grounding",
         "prompt_chat_agent", "console_assistant"}
    )
    assert "summarizer_ondevice" not in prompts.EDITABLE_KEYS
    assert "reranker_ondevice" not in prompts.EDITABLE_KEYS


def test_draft_prompts_imported_live_from_their_modules():
    """Draft prompt text is imported from the owning module so the console can never drift."""
    from maat import prompts
    from maat.agents.curation import _DRAFT_GEOTAG_PROMPT
    from maat.agents.triage import TRIAGE_LLM_PROMPT
    from maat.pipeline.grounding import GROUNDING_PROMPT
    from maat.serving.topics import _LLM_PROMPT_TEMPLATE

    assert prompts.PROMPTS_BY_KEY["topics_enrich"]["default"] == _LLM_PROMPT_TEMPLATE
    assert prompts.PROMPTS_BY_KEY["curation_geotag"]["default"] == _DRAFT_GEOTAG_PROMPT
    assert prompts.PROMPTS_BY_KEY["triage_llm"]["default"] == TRIAGE_LLM_PROMPT
    assert prompts.PROMPTS_BY_KEY["grounding"]["default"] == GROUNDING_PROMPT
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
    assert "live on the next run" in base and "built-in" in base  # title now in the shell topbar
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


def test_prompts_page_three_panel_nav_and_selected_agent():
    """3-panel Prompts: the LEFT nav lists every registered prompt (by label, grouped by status);
    the MIDDLE shows the SELECTED agent's full text — editable (save form) for active AND draft
    (#189), read-only only for on-device — and a non-selected agent's body is not dumped on the page."""
    import html as _html

    from maat import prompts
    from maat.web.app import _prompts_page

    # All drafts pending review (no events) — the realistic default state.
    review = {p["key"]: prompts.needs_review_given(set(), p["key"]) for p in prompts.PROMPTS}
    out = _prompts_page({}, selected="extract", review=review)

    # Left nav: every prompt is a selectable link, under the short group headings.
    for p in prompts.PROMPTS:
        assert _html.escape(p["label"]) in out, p["key"]
        assert f'/prompts?key={p["key"]}' in out, p["key"]
    for heading in ("Active", "Draft", "On-device"):
        assert heading in out

    # The selected ACTIVE agent shows its full seed text + the editable save form + golden test.
    ex_snip = _html.escape(prompts.PROMPTS_BY_KEY["extract"]["default"].strip().splitlines()[0])
    assert ex_snip in out
    assert 'action="/prompts/save"' in out and "Test on goldens" in out
    # A non-selected agent's body is NOT rendered in the middle (it's only a nav link).
    tri_snip = _html.escape(prompts.PROMPTS_BY_KEY["triage_llm"]["default"].strip().splitlines()[0])
    assert tri_snip not in out

    # #189: selecting a DRAFT agent shows its text in the SAME editable block (save form), plus the
    # 'needs review' tag + 'Mark reviewed' button. It's not a golden-eval prompt → no golden test.
    draft = _prompts_page({}, selected="triage_llm", review=review)
    assert tri_snip in draft
    assert 'action="/prompts/save"' in draft and "readonly" not in draft
    assert 'action="/prompts/reviewed"' in draft and "Mark reviewed" in draft
    assert "needs review" in draft
    assert "Test on goldens" not in draft

    # Selecting an ON-DEVICE agent shows its text read-only, no save form, edit-in-Apple note.
    od = _prompts_page({}, selected="reranker_ondevice", review=review)
    assert "readonly" in od and 'action="/prompts/save"' not in od
    assert "edit in the Apple app" in od
    # On-device entries are labelled as Foundation Models mirrors.
    assert "Foundation Models" in out


# --- #189: draft prompts are live + editable, carry a "needs review" tag a button clears ---------


class _FakeNats:
    """Captures the events the console publishes (subject, payload) without a live bus."""

    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    async def publish(self, subject, payload):
        self.published.append((subject, json.loads(payload.decode())))

    async def flush(self):
        pass


class _ReviewPool:
    """Pool stand-in: `reviewed` is the set of keys with an admin.prompt.reviewed event."""

    def __init__(self, reviewed):
        self._reviewed = set(reviewed)

    async def fetch(self, q, *args):
        return [{"key": k} for k in self._reviewed]  # review_map's distinct-key query

    async def fetchrow(self, q, *args):
        return (1,) if args and args[0] in self._reviewed else None  # needs_review's per-key probe


def test_needs_review_given_only_unreviewed_drafts():
    """Pure: a draft needs review until it's marked; active / on-device never carry the tag."""
    from maat import prompts

    assert prompts.needs_review_given(set(), "triage_llm") is True
    assert prompts.needs_review_given({"triage_llm"}, "triage_llm") is False
    assert prompts.needs_review_given(set(), "extract") is False  # active
    assert prompts.needs_review_given(set(), "reranker_ondevice") is False  # on-device


def test_review_map_and_needs_review_default_to_pending_without_pool():
    """No pool / un-migrated events table → every draft still needs review (the safe default)."""
    import asyncio

    from maat import prompts

    m = asyncio.run(prompts.review_map(None))
    assert m["triage_llm"] is True and m["topics_enrich"] is True
    assert m["extract"] is False and m["reranker_ondevice"] is False
    assert asyncio.run(prompts.needs_review(None, "triage_llm")) is True
    assert asyncio.run(prompts.needs_review(None, "extract")) is False


def test_review_map_reflects_reviewed_events():
    """Once a key has an admin.prompt.reviewed event, its tag clears; other drafts still pending."""
    import asyncio

    from maat import prompts

    pool = _ReviewPool({"triage_llm"})
    m = asyncio.run(prompts.review_map(pool))
    assert m["triage_llm"] is False  # reviewed → tag cleared
    assert m["topics_enrich"] is True  # still pending
    assert m["extract"] is False  # active, never tagged
    assert asyncio.run(prompts.needs_review(pool, "triage_llm")) is False
    assert asyncio.run(prompts.needs_review(pool, "curation_geotag")) is True


def _form_post(path: str, data: dict):
    """POST a form to the real ASGI app, NOT following the redirect; return (status, location)."""
    import asyncio

    import httpx

    from maat.web.app import app

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(path, data=data, follow_redirects=False)
            return r.status_code, r.headers.get("location", "")

    return asyncio.run(go())


def test_reviewed_route_publishes_prompt_reviewed_event(monkeypatch):
    """POST /prompts/reviewed clears a draft's tag by publishing admin.prompt.reviewed (key only).
    It carries no prompt text and nothing about activation — informational only."""
    from maat.web.app import app

    nats = _FakeNats()
    monkeypatch.setattr(app.state, "nats", nats, raising=False)
    status, loc = _form_post("/prompts/reviewed", {"key": "triage_llm", "reason": "looks good"})
    assert status == 303 and "/prompts" in loc
    subjects = [s for s, _ in nats.published]
    assert "maat.events.admin.prompt.reviewed" in subjects
    _, payload = next(p for p in nats.published if p[0].endswith("admin.prompt.reviewed"))
    assert payload["data"]["key"] == "triage_llm"
    assert "text" not in payload["data"]  # no prompt text, no activation field


def test_reviewed_route_rejects_ondevice(monkeypatch):
    """On-device prompts (Swift mirrors) carry no review action — the route rejects, publishes nothing."""
    from maat.web.app import app

    nats = _FakeNats()
    monkeypatch.setattr(app.state, "nats", nats, raising=False)
    status, _ = _form_post("/prompts/reviewed", {"key": "summarizer_ondevice"})
    assert status == 303
    assert nats.published == []


def test_write_routes_reject_ondevice(monkeypatch):
    """save / restore / test refuse on-device prompts (read-only Swift mirrors) and publish nothing."""
    from maat.web.app import app

    nats = _FakeNats()
    monkeypatch.setattr(app.state, "nats", nats, raising=False)
    status, _ = _form_post("/prompts/save", {"key": "reranker_ondevice", "text": "x", "reason": ""})
    assert status == 303
    status, _ = _form_post("/prompts/restore", {"key": "reranker_ondevice", "reason": ""})
    assert status == 303
    status, _ = _form_post("/prompts/test", {"key": "summarizer_ondevice", "text": "x"})
    assert status == 303
    assert nats.published == []


def test_no_activation_routes_exist():
    """#189 stayed SIMPLE: there is no approve-to-activate / deactivate machinery — only the review
    tag. The activation routes from the earlier design must not be registered."""
    from maat.web.app import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/prompts/reviewed" in paths
    assert "/prompts/activate" not in paths
    assert "/prompts/deactivate" not in paths


def test_page_draft_editable_with_review_tag_when_pending():
    """A pending draft renders the editable block + 'needs review' badge + 'Mark reviewed' button."""
    from maat import prompts
    from maat.web.app import _prompts_page

    review = {p["key"]: prompts.needs_review_given(set(), p["key"]) for p in prompts.PROMPTS}
    out = _prompts_page({}, selected="triage_llm", review=review)
    assert 'action="/prompts/save"' in out  # editable like any prompt
    assert "needs review" in out and 'action="/prompts/reviewed"' in out and "Mark reviewed" in out


def test_page_draft_middle_drops_tag_once_reviewed():
    """Once a draft is reviewed, its editor panel drops the tag + button (still editable)."""
    from maat import prompts
    from maat.web.app import _prompts_page

    review = {p["key"]: prompts.needs_review_given({"triage_llm"}, p["key"]) for p in prompts.PROMPTS}
    out = _prompts_page({}, selected="triage_llm", review=review)
    mid = out[out.find("p3-mid"):out.find("p3-right")]  # just the selected prompt's editor panel
    assert 'action="/prompts/save"' in mid  # still editable
    assert "Mark reviewed" not in mid and "needs review" not in mid  # tag cleared for this one


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
    assert "over time" in out  # page title now in the shell topbar; the §6 framing stays in-body
    assert "independent originator" in out  # the independence dimension is surfaced
    assert "confirmed" in out  # the truth-over-time outcome column
    assert "never" in out and "consensus" in out  # the anti-consensus framing is explicit
    # the lone extraordinary claimant is flagged, not silently scored
    assert "solo extraordinary" in out


def test_reputation_page_empty_history():
    from maat.web.app import _reputation_page

    out = _reputation_page([], 0)
    assert "No reputation yet" in out


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
    assert "needs sign-off" in out  # page title now in the shell topbar
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


def test_prompt_chat_rejects_only_ondevice_and_unknown_keys(monkeypatch):
    """On-device prompts (Swift mirrors) and unknown keys are not editable — chat refuses them and
    never calls the model. Backend drafts ARE editable now (#189), so chat works for them."""
    from maat.providers import seam

    def must_not_run(prompt, *, model="m", max_tokens=256):  # pragma: no cover
        raise AssertionError("claude_complete must not be called for a read-only key")

    monkeypatch.setattr(seam, "claude_complete", must_not_run)
    for key in ("summarizer_ondevice", "reranker_ondevice", "nonexistent"):
        status, data = _chat_post({"key": key, "current": "x", "messages": []})
        assert status == 200, key
        assert "error" in data and "editable" in data["error"].lower(), key


def test_prompt_chat_works_for_a_backend_draft(monkeypatch):
    """#189: a backend DRAFT prompt is editable, so 'Improve with Claude' is available on it — the
    route calls the model and returns its reply. (Marking it reviewed is a separate action.)"""
    from maat.providers import seam

    monkeypatch.setattr(
        seam, "claude_complete",
        lambda prompt, *, model="m", max_tokens=256: seam.Reply(text="refined", model=model),
    )
    status, data = _chat_post({"key": "triage_llm", "current": "the draft text", "messages": []})
    assert status == 200
    assert data.get("reply") == "refined"


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


def test_prompts_page_chat_panel_for_selected_editable_agent():
    """The always-open chat (right column) is wired to the SELECTED editable agent's editor — for
    active AND draft prompts (#189). An on-device selection shows the 'edit in the Apple app' note
    instead. Shared chat JS ships once."""
    from maat.web.app import _doc, _prompts_page

    page = _doc(_prompts_page({}, selected="extract"), "prompts", "prompts")
    assert "Improve with Claude" in page
    assert "maatPromptChat" in page  # the inline handler shipped
    # The selected editable agent has its editor + chat wired to the same key.
    assert 'id="ta-extract"' in page and 'id="in-extract"' in page and 'id="log-extract"' in page
    assert "maatPromptChat('extract')" in page
    # A non-selected agent's editor/chat is NOT on the page (it's just a nav link).
    assert 'id="ta-classify"' not in page

    # Selecting another editable agent wires that one instead.
    page2 = _doc(_prompts_page({}, selected="classify"), "prompts", "prompts")
    assert 'id="ta-classify"' in page2 and "maatPromptChat('classify')" in page2

    # #189: a DRAFT selection is editable too — editor textarea + wired chat panel.
    draft = _doc(_prompts_page({}, selected="triage_llm"), "prompts", "prompts")
    assert 'id="ta-triage_llm"' in draft and "maatPromptChat('triage_llm')" in draft

    # An ON-DEVICE selection: no editable textarea, shows the edit-in-Apple note.
    od = _doc(_prompts_page({}, selected="reranker_ondevice"), "prompts", "prompts")
    assert 'id="ta-reranker_ondevice"' not in od
    assert "edit in the Apple app" in od


def test_prompt_chat_agent_prompt_in_registry_as_reviewable_draft():
    """#159/#189: the chat helper's own instructions are surfaced in the registry as a draft with a
    description, so cauri can review them — and like every backend draft they are editable (not
    on-device, so in EDITABLE_KEYS)."""
    from maat import prompts

    entry = prompts.PROMPTS_BY_KEY["prompt_chat_agent"]
    assert entry["status"] == "draft"
    assert entry["source"]
    assert len(entry["description"]) > 30
    assert entry["default"].strip()  # the actual instructions are surfaced for review
    assert "prompt_chat_agent" in prompts.EDITABLE_KEYS  # #189: reviewed + editable like any draft
    # The instructions keep the substitution points the console fills at chat time.
    for tok in ("{prompt_label}", "{prompt_purpose}", "{current_prompt}"):
        assert tok in prompts.PROMPT_CHAT_AGENT


def test_settings_page_uses_smart_inputs_and_tooltips():
    """Model knobs render a <select> with the current model selected; numeric knobs render number
    fields (not free text); every knob carries an on-page (data-tip) explanation."""
    from maat.web.app import _config_page

    out = _config_page({})
    assert '<select name="value"' in out                       # model knob -> dropdown
    assert "claude-haiku-4-5-20251001" in out and "selected" in out  # current model preselected
    assert 'type="number"' in out and 'step="0.01"' in out and 'step="1"' in out  # float + int fields
    assert 'placeholder="new value"' not in out               # the dumb free-text box is gone
    assert 'class="tip" data-tip=' in out                      # hover help on the page


def test_doc_shell_is_sidebar_with_external_stylesheet():
    """The shell is the sidebar layout, styled from the central /static/console.css (no inline CSS)."""
    from maat.web.app import _doc

    page = _doc("<p>x</p>", "sub", "config")
    assert 'rel="stylesheet" href="/static/console.css"' in page
    assert "<style>" not in page                                # CSS is external now
    assert 'class="app"' in page and 'class="sidebar"' in page  # sidebar layout
    assert "data-tip=" in page                                  # nav items carry on-page tooltips


def test_assistant_panel_and_prompt_wired():
    """The always-open assistant renders with the page context on normal pages, is hidden on
    Prompts (its own chat), and its system prompt is an editable registry prompt with placeholders."""
    from maat import prompts
    from maat.web.app import _assistant_prompt, _doc

    page = _doc("<p>x</p>", "sub", "config")
    assert 'class="assistant"' in page and 'data-page="Settings"' in page
    assert 'id="asst-in"' in page and "maatAssistant" in page
    assert 'class="assistant"' not in _doc("x", "s", "prompts")  # Prompts keeps its own chat

    assert prompts.PROMPTS_BY_KEY["console_assistant"]["status"] == "draft"  # reviewable, code-canonical
    assert prompts.PROMPTS_BY_KEY["console_assistant"]["placeholders"] == ["{page}", "{purpose}"]
    assert _assistant_prompt("Settings", "the dials", [], "On {page}: {purpose}") == "On Settings: the dials"


def test_activity_has_one_run_button_not_per_step_logs():
    """Activity shows a single 'Run the pipeline' control + per-step progress pills; the old
    per-stage 'Log a run' buttons (which didn't actually run anything) are gone."""
    from maat.web.app import _run_state, _runs_page, stage_summary

    page = _runs_page(stage_summary({}), {"articles": 0, "claims": 0, "clusters": 0}, [], [])
    assert "run-btn" in page and "maatRunPipeline()" in page          # one run button
    assert 'id="rs-0"' in page and 'id="rs-3"' in page                # 4 progress pills
    assert "Log a run" not in page and "/runs/trigger" not in page    # old per-step logs removed
    steps = _run_state()["steps"]
    assert len(steps) == 4 and steps[0]["label"] == "Find articles"   # the 4 pipeline steps
