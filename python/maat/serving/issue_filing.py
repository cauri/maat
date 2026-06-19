"""Feedback → tracked issue (P7, #214) — the step that closes the loop.

The pipeline already records user feedback (`feedback.submitted`) and triages it
(`feedback.triaged`, routing `bug`/`ui` and operator-confirmed items to `auto-fix`).
This module turns an `auto-fix`-routed item into an actual TRACKED ISSUE, deduping repeats
and back-linking via a new `feedback.linked` event so the admin/operator can see status.

Design (event-sourced, untrusted-input-safe):
- We only act on items the system or a human already ROUTED to `auto-fix` (#188 operator
  action, or triage's mechanical bug/ui flag) — never on raw feedback text. So a feedback
  burst can't spam the tracker on its own (#77).
- Dedup: similar feedback collapses to ONE issue by a stable key (category + the first
  significant words); repeats attach to the existing issue instead of spawning duplicates.
- Filing is GitHub-token-gated. With `MAAT_GH_ISSUE_TOKEN` + `MAAT_GH_REPO` set, it creates a
  real GitHub issue. WITHOUT a token it records the link as `proposed` — the worthiness +
  dedup decision is still made and surfaced, and the operator files from /review (or a token
  is added later, at which point proposed items get filed). So this ships with no new prod
  secret and no risk of unattended issue-creation until cauri opts in.

Run (standalone; safe to schedule like triage):
    uv run python -m maat.serving.issue_filing            # dry-run unless a token is configured
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from maat.events import envelope, publish

FEEDBACK_LINKED = "feedback.linked"

# Only these existing repo labels are applied (GitHub 422s on unknown labels); the category is
# carried in the title/body. "enhancement" exists in cauri/maat; bug maps to the bug label.
_CATEGORY_LABEL = {"bug": "bug"}
_BASE_LABELS = ("enhancement",)


# ---------------------------------------------------------------------------
# Pure core (no I/O) — fully testable
# ---------------------------------------------------------------------------


def issue_worthy(route: str, auto_fixable: bool = False) -> bool:
    """Does a triaged item warrant a tracked issue? Only the `auto-fix` decision — made by
    triage (mechanical bug/ui) or by a human operator (#188) — qualifies. Review/dispute items
    stay for a person; they become issue-worthy only once an operator routes them to auto-fix."""
    return route == "auto-fix" or bool(auto_fixable)


_STOP = {"the", "and", "for", "that", "this", "with", "not", "but", "you", "are", "was"}


def dedup_key(category: str, text: str) -> str:
    """Stable cluster key so near-identical reports collapse to one issue. Category + a hash of
    the first significant words (>=3 chars, minus a few stopwords). Heuristic by design — it
    over-merges rather than spawns duplicates, which is the safer error for a tracker."""
    toks = [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) >= 3 and t not in _STOP]
    base = " ".join(toks[:8])
    cat = (category or "feedback").strip().lower()
    return f"{cat}::{hashlib.sha1(base.encode()).hexdigest()[:12]}"


def build_issue(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the GitHub issue payload (title/body/labels) for a cluster of feedback items."""
    first = items[0]
    cat = (first.get("triage") or {}).get("category") or first.get("category_hint") or "feedback"
    lead = (first.get("text") or "user feedback").strip().replace("\n", " ")
    title = f"[feedback:{cat}] {lead[:70]}".rstrip()
    n = len(items)
    lines = [
        f"Auto-filed from user feedback triaged to **auto-fix** (category: `{cat}`).",
        f"\n**{n} report{'s' if n != 1 else ''}** in this cluster:\n",
    ]
    for it in items:
        tri = it.get("triage") or {}
        conf = tri.get("confidence")
        src = it.get("source") or "?"
        lines.append(
            f"- {(it.get('text') or '').strip()!r} "
            f"_(item `{it.get('item_id', '?')}`, source `{src}`"
            + (f", conf {conf:.2f}" if isinstance(conf, (int, float)) else "")
            + ")_"
        )
    lines.append("\n_Filed by the feedback loop (#214). De-duplicated by category + lead text._")
    labels = list(_BASE_LABELS)
    if cat in _CATEGORY_LABEL:
        labels.append(_CATEGORY_LABEL[cat])
    return {"title": title, "body": "\n".join(lines), "labels": labels}


# ---------------------------------------------------------------------------
# GitHub filing (I/O) — token-gated
# ---------------------------------------------------------------------------


def file_issue_github(payload: dict[str, Any], *, repo: str, token: str) -> tuple[int, str]:
    """Create a GitHub issue; return (number, html_url). Raises on HTTP error / missing token."""
    if not (repo and token):
        raise ValueError("file_issue_github requires repo + token")
    r = httpx.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"title": payload["title"], "body": payload["body"], "labels": payload.get("labels", [])},
        timeout=20,
    )
    r.raise_for_status()
    d = r.json()
    return int(d["number"]), str(d.get("html_url", ""))


# ---------------------------------------------------------------------------
# Event I/O — feedback.linked (back-link; kept local to avoid touching feedback.py)
# ---------------------------------------------------------------------------


async def record_linked(
    pool: Any,
    nc: Any | None,
    *,
    item_id: str,
    issue_ref: str,
    issue_url: str = "",
    dedup_key: str = "",
    status: str = "filed",  # filed | attached | proposed
    tenant_id: str = "cauri",
) -> None:
    """Publish a `feedback.linked` event tying a feedback item to the issue it produced."""
    data = {
        "item_id": item_id,
        "issue_ref": issue_ref,
        "issue_url": issue_url,
        "dedup_key": dedup_key,
        "status": status,
    }
    if nc is not None:
        await publish(nc, FEEDBACK_LINKED, item_id, data, tenant_id)
    else:
        payload = json.loads(envelope(item_id, FEEDBACK_LINKED, data, tenant_id))
        await pool.execute(
            "insert into events (stream_id, type, data, tenant_id) values ($1,$2,$3,$4)",
            payload["stream_id"], payload["type"], json.dumps(payload["data"]), payload["tenant_id"],
        )


@dataclass
class LinkedState:
    handled: set[str]                 # item_ids already filed or attached (skip)
    proposed: set[str]                # item_ids recorded as proposed (don't re-propose, but DO file once a token exists)
    key_to_issue: dict[str, dict]     # dedup_key -> {issue_ref, issue_url} for already-filed clusters


async def linked_state(pool: Any, *, tenant_id: str = "cauri") -> LinkedState:
    """Read prior `feedback.linked` events into the dedup/skip state. Latest event per item wins."""
    rows = await pool.fetch(
        "select data from events where type = $1 and tenant_id = $2 order by id asc",
        FEEDBACK_LINKED, tenant_id,
    )
    status_by_item: dict[str, str] = {}
    key_to_issue: dict[str, dict] = {}
    for r in rows:
        d = json.loads(r["data"]) if isinstance(r["data"], str) else dict(r["data"])
        iid = d.get("item_id")
        if iid:
            status_by_item[iid] = d.get("status", "filed")  # later rows overwrite → latest wins
        key, ref = d.get("dedup_key"), d.get("issue_ref")
        if key and ref and ref != "proposed":
            key_to_issue.setdefault(key, {"issue_ref": ref, "issue_url": d.get("issue_url", "")})
    handled = {i for i, s in status_by_item.items() if s in ("filed", "attached")}
    proposed = {i for i, s in status_by_item.items() if s == "proposed"}
    return LinkedState(handled=handled, proposed=proposed, key_to_issue=key_to_issue)


# ---------------------------------------------------------------------------
# Runner — read auto-fix queue → dedup → file/attach/propose → back-link
# ---------------------------------------------------------------------------


async def run(
    pool: Any,
    nc: Any | None,
    *,
    repo: str = "",
    token: str = "",
    tenant_id: str = "cauri",
) -> list[dict[str, Any]]:
    """Process `auto-fix`-routed feedback into tracked issues. Returns a per-cluster summary.

    For each cluster of un-handled items: attach to the existing issue if this dedup key already
    filed one; else file a new GitHub issue when a token is configured; else record it `proposed`
    for the operator. Every processed item gets a `feedback.linked` event.
    """
    from maat.serving.feedback import routed_queue

    auto = await routed_queue(pool, route="auto-fix", tenant_id=tenant_id)
    state = await linked_state(pool, tenant_id=tenant_id)

    # Cluster the un-handled items by dedup key.
    clusters: dict[str, list[dict]] = {}
    for it in auto:
        iid = it.get("item_id")
        if not iid or iid in state.handled:
            continue
        cat = (it.get("triage") or {}).get("category") or it.get("category_hint") or "feedback"
        clusters.setdefault(dedup_key(cat, it.get("text") or ""), []).append(it)

    summary: list[dict[str, Any]] = []
    for key, items in clusters.items():
        if key in state.key_to_issue:  # cluster already has an issue → attach the new reports
            ref = state.key_to_issue[key]
            for it in items:
                await record_linked(pool, nc, item_id=it["item_id"], issue_ref=ref["issue_ref"],
                                    issue_url=ref.get("issue_url", ""), dedup_key=key,
                                    status="attached", tenant_id=tenant_id)
            summary.append({"key": key, "status": "attached", "issue_ref": ref["issue_ref"], "items": len(items)})
        elif repo and token:  # file a brand-new issue
            num, url = file_issue_github(build_issue(items), repo=repo, token=token)
            for it in items:
                await record_linked(pool, nc, item_id=it["item_id"], issue_ref=str(num),
                                    issue_url=url, dedup_key=key, status="filed", tenant_id=tenant_id)
            state.key_to_issue[key] = {"issue_ref": str(num), "issue_url": url}
            summary.append({"key": key, "status": "filed", "issue_ref": str(num), "items": len(items)})
        else:  # no token → record a proposal (once) for the operator to file
            fresh = [it for it in items if it["item_id"] not in state.proposed]
            for it in fresh:
                await record_linked(pool, nc, item_id=it["item_id"], issue_ref="proposed",
                                    dedup_key=key, status="proposed", tenant_id=tenant_id)
            if fresh:
                summary.append({"key": key, "status": "proposed", "issue_ref": "proposed", "items": len(fresh)})
    return summary


async def _main() -> None:
    from dotenv import load_dotenv
    from pathlib import Path

    from maat.bus import connect
    from maat.db import get_pool

    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
    pool = await get_pool()
    nc = await connect()
    repo = os.environ.get("MAAT_GH_REPO", "")
    token = os.environ.get("MAAT_GH_ISSUE_TOKEN", "")
    summary = await run(pool, nc, repo=repo, token=token)
    await nc.flush()
    await nc.close()
    await pool.close()
    filed = sum(s["items"] for s in summary if s["status"] == "filed")
    proposed = sum(s["items"] for s in summary if s["status"] == "proposed")
    attached = sum(s["items"] for s in summary if s["status"] == "attached")
    mode = "GitHub" if (repo and token) else "proposed-only (no MAAT_GH_ISSUE_TOKEN/MAAT_GH_REPO)"
    print(f"[issue-filing] {mode}: filed={filed} attached={attached} proposed={proposed} "
          f"across {len(summary)} cluster(s)", flush=True)


def main() -> None:
    import asyncio

    asyncio.run(_main())


if __name__ == "__main__":
    main()
