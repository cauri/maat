"""Tests for the feedback → tracked-issue engine (#214). Pure logic + mocked GitHub + the
dedup/attach/propose runner — no real DB or network."""

from __future__ import annotations

import asyncio

from maat.serving import issue_filing as isf
from maat.serving.issue_filing import LinkedState


def _item(item_id, text, category="ui", route="auto-fix", conf=0.82):
    return {
        "item_id": item_id,
        "text": text,
        "source": "reader-app",
        "category_hint": category,
        "triage": {"category": category, "route": route, "confidence": conf},
    }


def test_issue_worthy_only_on_auto_fix():
    assert isf.issue_worthy("auto-fix") is True
    assert isf.issue_worthy("review") is False
    assert isf.issue_worthy("resolved") is False
    assert isf.issue_worthy("review", auto_fixable=True) is True  # operator escalation


def test_dedup_key_clusters_same_text_splits_different():
    a1 = isf.dedup_key("ui", "Login button does nothing when tapped")
    a2 = isf.dedup_key("ui", "Login button does nothing when tapped")
    b = isf.dedup_key("ui", "Dark mode colours look wrong everywhere")
    assert a1 == a2          # identical reports collapse
    assert a1 != b           # different reports don't
    # category participates in the key
    assert isf.dedup_key("bug", "Login button does nothing when tapped") != a1


def test_build_issue_payload_shape():
    items = [_item("fb-1", "Login button does nothing"), _item("fb-2", "Login button dead")]
    p = isf.build_issue(items)
    assert p["title"].startswith("[feedback:ui]")
    assert "2 reports" in p["body"]
    assert "fb-1" in p["body"] and "fb-2" in p["body"]
    assert "enhancement" in p["labels"]          # only existing repo labels
    # a bug cluster also carries the bug label
    assert "bug" in isf.build_issue([_item("fb-3", "crash on open", category="bug")])["labels"]


def test_file_issue_github_mocked(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"number": 321, "html_url": "https://github.com/cauri/maat/issues/321"}

    def fake_post(url, **kw):
        captured["url"] = url
        captured["json"] = kw.get("json")
        return _Resp()

    monkeypatch.setattr(isf.httpx, "post", fake_post)
    num, url = isf.file_issue_github({"title": "t", "body": "b", "labels": ["enhancement"]},
                                     repo="cauri/maat", token="tok")
    assert num == 321 and url.endswith("/321")
    assert captured["url"].endswith("/repos/cauri/maat/issues")
    assert captured["json"]["title"] == "t"


def test_file_issue_github_requires_token():
    import pytest

    with pytest.raises(ValueError):
        isf.file_issue_github({"title": "t", "body": "b"}, repo="", token="")


def _run(monkeypatch, *, items, state, repo="", token=""):
    """Drive run() with the two reads + the writer faked; return the captured linked-events."""
    calls: list[dict] = []

    async def fake_routed_queue(pool, *, route, limit=200, tenant_id="cauri"):
        assert route == "auto-fix"
        return items

    async def fake_linked_state(pool, *, tenant_id="cauri"):
        return state

    async def fake_record_linked(pool, nc, *, item_id, issue_ref, issue_url="",
                                 dedup_key="", status="filed", tenant_id="cauri"):
        calls.append({"item_id": item_id, "issue_ref": issue_ref, "status": status, "dedup_key": dedup_key})

    monkeypatch.setattr("maat.serving.feedback.routed_queue", fake_routed_queue)
    monkeypatch.setattr(isf, "linked_state", fake_linked_state)
    monkeypatch.setattr(isf, "record_linked", fake_record_linked)
    summary = asyncio.run(isf.run(None, None, repo=repo, token=token))
    return summary, calls


def test_run_proposes_without_token_and_dedups(monkeypatch):
    items = [
        _item("fb-1", "Login button does nothing when tapped"),
        _item("fb-2", "Login button does nothing when tapped"),  # same text → same cluster
        _item("fb-3", "Dark mode colours look wrong everywhere"),
    ]
    summary, calls = _run(monkeypatch, items=items, state=LinkedState(set(), set(), {}))
    # two clusters; every item recorded as a proposal (no token)
    assert {c["status"] for c in calls} == {"proposed"}
    assert len(calls) == 3
    assert sorted(s["items"] for s in summary) == [1, 2]   # cluster of 2 + cluster of 1
    assert all(s["status"] == "proposed" for s in summary)


def test_run_files_issue_when_token_present(monkeypatch):
    monkeypatch.setattr(isf, "file_issue_github", lambda payload, *, repo, token: (777, "u/777"))
    items = [_item("fb-1", "crash on open", category="bug"), _item("fb-2", "totally different report here", category="ui")]
    summary, calls = _run(monkeypatch, items=items, state=LinkedState(set(), set(), {}),
                          repo="cauri/maat", token="tok")
    assert {c["status"] for c in calls} == {"filed"}
    assert all(c["issue_ref"] == "777" for c in calls)
    assert all(s["status"] == "filed" for s in summary)


def test_run_attaches_to_existing_issue_for_known_cluster(monkeypatch):
    text = "Login button does nothing when tapped"
    key = isf.dedup_key("ui", text)
    state = LinkedState(handled=set(), proposed=set(),
                        key_to_issue={key: {"issue_ref": "50", "issue_url": "u/50"}})
    items = [_item("fb-9", text), _item("fb-10", "an unrelated new complaint about spacing")]
    summary, calls = _run(monkeypatch, items=items, state=state)
    attached = [c for c in calls if c["status"] == "attached"]
    assert attached and all(c["issue_ref"] == "50" for c in attached)
    assert any(s["status"] == "attached" and s["issue_ref"] == "50" for s in summary)
    # the unrelated item is a new cluster → proposed (no token)
    assert any(s["status"] == "proposed" for s in summary)


def test_run_skips_already_handled_items(monkeypatch):
    items = [_item("fb-1", "crash on open", category="bug")]
    state = LinkedState(handled={"fb-1"}, proposed=set(), key_to_issue={})
    summary, calls = _run(monkeypatch, items=items, state=state)
    assert calls == [] and summary == []   # already filed/attached → nothing to do
