# Spike: Auto-generate draft PRs for auto-fixable feedback

**Issue:** #216 — (spike) Auto-generate draft PRs for auto-fixable feedback
**Informs:** the `auto-fix` branch of #58, building on #214 (feedback → tracked issue)
**Part of:** #7 (P7 — Feedback loop)
**Status:** DECIDED — **defer always-on automation; ship a gated, operator-triggered dispatcher after #214 + #210 are live**
**Author:** agent (2026-06-16)

---

## 1. The question

Triage flags clear bugs / trivial UI reports as `auto_fixable` ("safe to generate a PR
without human sign-off"). #214 now turns those into tracked **issues**. Should the loop go one
step further and **auto-generate a draft PR** that fixes them — feedback → issue → fix → PR —
and if so, how, and how safely?

## 2. Current state (what exists after #214)

```
user feedback ──▶ feedback.submitted ──▶ triage ──▶ feedback.triaged {route: auto-fix}
                                                          │
                                              #214 issue_filing.run()
                                                          ▼
                                        GitHub issue (deduped) + feedback.linked
```

What does **not** exist: anything that reads that issue and produces a code change. The
`auto_fixable` flag and the filed issue are where the automation currently stops.

## 3. Proposed end-to-end flow (the thing this spike evaluates)

```
feedback.linked {status: filed, issue_ref} ──▶ dispatcher ──▶ coding agent on a branch
                                                                    │
                                                          draft PR  ▼  "Closes #<issue>"
                                                       CI gates run; human reviews; human merges
```

## 4. Approaches considered

| # | Mechanism | Pros | Cons |
|---|-----------|------|------|
| A | **GitHub Action** triggered on an issue label (e.g. `feedback-autofix`), runs a coding agent in CI, opens a draft PR | runs off-box (no prod secret on the reader); native to the repo; CI-sandboxed | needs an agent runner + model key in Actions; cold-start; Action has write scope |
| B | **On-box dispatcher** in the scheduled tick: after #214 files an issue, call a coding agent to produce a patch and open a PR | co-located with the loop | puts a GitHub *write* token + model spend on the production box; couples prod to the dev repo; worst abuse surface |
| C | **Operator-triggered** from `/review`: a "Draft a fix" button dispatches the agent (A's runner) for one issue | human in the loop by construction; no always-on automation; easiest to reason about | one click of latency (which is the point) |
| D | **Templated/scripted fixes** for a tiny set of known-shape issues (e.g. a copy string, a colour token) | deterministic, no model | covers almost nothing; most "auto-fixable" UI/bug reports aren't template-shaped |

## 5. Safety / cost / abuse analysis

- **Untrusted input (#77).** The trigger chain originates in public user feedback. Even gated
  on the `auto-fix` route, an attacker who can get text classified as `bug`/`ui` could, in an
  always-on design (B), drive the box to spend model tokens and open PRs at will. The #214
  dedup + threshold mitigates issue spam; it does **not** make unattended code-generation safe.
- **Blast radius.** A draft PR is low-risk *if and only if* it can never auto-merge and CI
  gates always run. Both are easy to guarantee (open as `draft`, no auto-merge workflow, branch
  protection on `main`). The real cost is reviewer attention on low-quality patches.
- **Cost.** Each attempt is a multi-step agent run (≫ a single LLM call). Bounded only by issue
  volume, which is bounded by #214's dedup + the `auto-fix` gate — acceptable for operator-
  triggered (C), unbounded-ish for always-on (B).
- **Secret placement.** A & C keep the GitHub *write* token in GitHub Actions (scoped,
  rotatable, off the box). B puts it on the production reader — avoid.

## 6. Guardrails any version must have

1. **Draft only. Never auto-merge.** `main` stays branch-protected; the full CI gate runs.
2. **One issue → one branch → one draft PR**, referencing the issue (`Closes #N`); re-runs
   update the same PR, never fan out.
3. **Scope cap:** refuse if the proposed diff touches > N files or > M lines, or touches
   anything outside an allowlist (e.g. not `rust/`, not migrations, not CI, not secrets).
4. **Rate limit** per day, independent of feedback volume.
5. **Label/age gate:** only act on an issue that has survived triage + (ideally) a brief
   operator window, so a mis-triaged burst doesn't immediately generate PRs.
6. **Provenance in the PR body:** link the originating feedback item(s) so a reviewer can judge.

## 7. Recommendation — **DEFER always-on; build gated, in this order**

1. **Now:** stop at #214 (issue is filed + deduped + back-linked). That already delivers "the
   feedback was scanned and an issue created."
2. **Next (small, safe):** **operator-triggered** dispatch — approach **C** — a "Draft a fix"
   action on `/review` that fires approach **A**'s GitHub-Action runner for a single issue. Human
   in the loop, no prod secret, no always-on surface. This is the right first build once there is
   real feedback flowing (needs **#210** for users to submit, and #214 merged to file issues).
3. **Later, only if warranted by real data:** consider an always-on dispatcher with the §6
   guardrails and a confidence/age threshold — but only after we've seen that operator-triggered
   draft PRs are actually good enough to be worth automating.

**Do not** build approach B (on-box, always-on). The combination of untrusted input, a
production GitHub write token, and unbounded model spend is not worth the convenience.

## 8. Smallest next step

A `repository_dispatch` GitHub Action (`autofix-issue`) that takes an issue number, runs a
coding agent in CI on a fresh branch, and opens a **draft** PR with the §6 guardrails — invoked
manually first (`gh workflow run`), then wired to a `/review` button. No production code or
secret involved until that button ships. Track as a follow-up issue under #7 when prioritised.
