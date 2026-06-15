# Maat — Timeline

A running narrative log of what happened and *why we changed course* — including dead-ends and
reversals (normal git history loses the "why we abandoned X"). One entry per meaningful day/decision
point. Newest at top.

---

## 2026-06-15 — P8 operator console + prompt governance

Built the **admin/operator console** (epic #66) — the reader evolved into an operator surface to run,
observe, and **correct** the veracity engine. Shipped F1–F5, A1, A2, A4a: claim/cluster inspectors +
corrections (split/merge/move, classification, §5.2 laundering flags), run/activity + dead-letters,
ingestion-clock pause, source registry, config + eval (cat-cafe) surfacing. Admin actions are **typed
events** on the same log (audit + replay free, D5/D20); corrections double as golden-corpus / RL signal.
Then a plain-language pass (tabs renamed — **Clocks→Updates**, Content→Feed, Runs→Activity,
Config→Settings, Eval→Quality, Audit→History — tooltips, after-action confirmations). See D28.

- **Course-change worth remembering — prompt governance.** cauri asked to edit agent prompts directly
  in the console, which cut against the repo's "prompts live in code" convention (the `claude-review`
  note). Resolved (D29, cauri chose "Option B"): code stays the **canonical seed**; the console saves
  **event-sourced operator overrides** the agents read at run time (live on next run), versioned +
  rollback + placeholder-guard + **eval-on-change** (`make eval-prompt`). The convention is deliberately
  **relaxed for operator overrides only** — not arbitrary external prompts; content still co-designed
  with cauri; edits operator-driven, never agent self-modification.
- **Testing pivot (D30):** added a Postgres-backed route integration harness and put it on the CI gate
  (`services: postgres`) — deterministic DB integration belongs on the gate (D16 was about live LLMs,
  not infra). It raises rather than skips in CI without a DB.
- **Gotcha for other agents:** the web app reads projections the **kernel** owns; if kerneld hasn't
  applied a new migration, a page reading the new table 500s. Fixed Activity/Prompts to **degrade, not
  500**, when `dead_letters`/`prompts` are missing — but the real unblock is **rebuild + restart
  `maat-kerneld`** so it applies migrations on startup. Restart kerneld after merging a migration.
- Read-only **Gamelan** prompt comparison (no port): Gamelan models the system prompt as self-adapting
  event-sourced *working memory*; Maat keeps it code-canonical + operator-gated with an eval net — an
  intentional divergence (don't let the core drift unsupervised). Possible future inspiration: prompt
  *composition* (base + memory + per-call) vs one flat template per stage.
- Issue hygiene: backfilled closed issues for previously-untracked work (#124–#127) under epic #66.

## 2026-06-15 — Client UX reframe: Apple-News reading model + Sources reputation

The P6 client first shipped veracity-dashboard-first; cauri reframed it to the brief's intent (§1:
"closer to Apple News in feel") — a reading app, with news-organisation reputation (§6) as a co-equal
surface. Re-grounded in `BRIEF.md` before redesigning (D25).

- **Today** now reads like Apple News: featured lead + scrollable list, the corroborated fact as the
  headline, a *quiet* confidence cue, independent originators surfaced first (§5.5). Claim-level
  veracity moved behind a "Why this confidence" disclosure.
- **Sources** (new hero): newsrooms ranked by reputation (truthfulness, one scalar §6.2) with a
  trajectory sparkline (§6.4); cold-start shown neutrally (§6.6). Reputation also shows inline per
  source while reading.
- IA: tabs Today · Sources · Search · Following (pins + topics); Settings → gear in Today.
- **Reputation is a provisional proxy** — the §6 truth-over-time fold is P3 (#37), not built; the
  reader's `/api/sources` approximates it from corroboration + primary standing, clearly labelled.
- **Not a reversal of the engine** — only the client's presentation changed. The veracity core stands.

**Next:** real reputation when #37 lands; a source-reputation App Intent; lead-story imagery once
acquisition pulls media.

## 2026-06-14 (later) — P0 shipped + deploy path proven

- **P0 foundations** committed/pushed to github.com/cauri/maat; first CI run green (17s).
  Rust kernel (event-sourcing fold + determinism tests), Python provider seam (Claude/Mistral,
  live smoke passing), local Postgres+pgvector, deterministic CI.
- **Deploy path proven end-to-end on Hetzner** (cx23, Falkenstein / fsn1, EU): cloud-init Docker
  install → docker-compose (Postgres+**pgvector 0.8.2** + **NATS JetStream**) → all healthy.
- Gotchas captured for next time: ARM `cax11` was capacity-constrained in fsn1 (fell back to x86
  `cx23`); Hetzner's Intel small type is `cx23`, not `cx22`; the Bash tool runs **zsh** (no
  unquoted-variable word-splitting — quote/array SSH opts).
- Spec added verbatim as `BRIEF.md`; task tracking = **GitHub issues** (P1–P7).

## 2026-06-14 — Design conversation; plan set

Worked the brief into an architecture and a build plan through discussion (no code yet).

- Settled the full architecture and stack — see `DECISIONS.md` D1–D19 and `PLAN.md`.
- Surveyed **gamelan** (cauri's own framework, inspiration-only/IP-protected) with read-only scouts:
  adopted its substrate patterns (Source/Effect seam, event-sourced folds, Check/Verdict gate,
  bounded self-modification, verification practices); confirmed it has **zero** veracity domain logic
  — the core is ours to invent.
- Assessed **cat-cafe** (Apache-2.0): adopted for observability + immediate-eval; the longitudinal
  truth-resolution / calibration / RL eval is built natively (the event log owns it).
- **Reversals worth remembering:** I twice over-hardened cauri's *leans* into *rules* (the
  "judgement→agent" lean; a "veracity firewall" around engagement data cauri never asked to govern).
  Corrected: hold leans as leans; engagement capture is **collection-only**, meaning TBD by analysis.
- An earlier unilateral `PLAN.md` + `ba` epics were set aside as scratch and superseded by this plan.

**Next:** cauri provisions host (Hetzner) + GitHub + keys; then P0 foundations → P1 veracity-core
slice on a small corpus.
