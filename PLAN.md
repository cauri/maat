# Maat — Build Plan

> **Maat** (Ma'at, "mah-AHT"; no apostrophe in repo/CLI/code) — a multi-tenant, continuously-warm
> personal news feed that scores **corroboration over spread**, attaches a **confidence read** to
> every story, and pushes hard *against* Anglo-American slant. The **claim**, not the article, is the
> unit of everything. Reads natively on iPhone + Mac (iOS 27).
>
> This is the **living** build plan. `BRIEF.md` (the original product spec) is authoritative for
> product *why*; `DECISIONS.md` is the decision record (the *why* behind each engineering call);
> `TIMELINE.md` logs pivots and reversals. Expect this plan to change — the brief says so, and
> several pieces are deliberately left to be **learned by building**, not designed on paper.
>
> _Last updated 2026-06-14._

---

## 1. The goal

Escape both the region-lock and the American centre-of-gravity of existing news apps, and trust
**corroborated truth over loud consensus**. A continuous feed that stays warm through the day,
ranges across the open web in any language, gates only the genuinely untrustworthy, and labels
everything else with a confidence read. Quality over cost throughout. Serves multiple users from day
one. Seed topics: world politics and AI.

---

## 2. Architecture

### 2.1 Compute tiers
- **Tier 1 — Server (frontier reasoning).** The veracity core, both agents, the canonical store, the
  two clocks, the RL loop. Runs while devices sleep; carries every hard problem.
- **Tier 2 — On-device (Apple Foundation Models, Swift).** Re-rank the served feed against the
  reader's natural-language topics, summarise-to-taste, on-device semantic search, hold comments
  locally, edge-aggregate analytics. Free, private, offline.
- **Tier 3 — Private Cloud Compute.** "Go deeper on this story" middle tier / fallback.

### 2.2 Agents — peer-to-peer, **no orchestrator**
- **Cast** (Egyptian-myth, weighing-of-the-heart): **Anubis** = corroboration/veracity scorer;
  **Thoth** = canonical-store scribe (mechanical); an **Assessor** = attribution-vs-assertion +
  fact-vs-projection + laundering-guard judgement (spawns sub-assessors on demand); **Ammit** = the
  veracity gate (suppress below-floor). Plus **acquisition** (where to look) and **curation** (what
  reaches the reader). No Osiris — there is no central conductor.
- **Coordination = choreography, not orchestration.** Two channels: forward flow via events on the
  shared store (acquisition writes → assessors react → scorer reacts → curation reacts), and the
  **backward asks** that are the real coordination (Anubis messages acquisition "find independent
  corroboration for X"; curation tells acquisition a topic is underserved).
- **Decomposition rule (a lean, not a law):** judgement → agent; mechanical/deterministic → tool.
  Any agent can **spawn sub-agents on demand**; decomposition is a runtime decision, not a fixed
  roster (the "42 Assessors" emerge as needed).
- **A2A** the protocol is deferred until a real cross-process / cross-trust boundary exists; the
  agent *boundaries* are logical for now, coordinating in-process over the bus.

### 2.3 The kernel contract (borrowed pattern, re-derived)
- **The Rust kernel returns effects as typed data; the Python rim performs the I/O.** The kernel
  computes "what to do next" as values; the agent layer executes them. Deterministic, replayable,
  testable by construction.
- **One uniform "Source/Effect" seam:** LLMs, tools, MCP servers, and sub-agents are all the same
  kind of effect behind one dispatch. This single seam delivers the Claude/Mistral split, MCP tools,
  and sub-agent spawning.
- **The Check/Verdict gate = the veracity gate (§5.7):** transform-enrich every claim with a
  `{confidence, provenance}` envelope; suppress only the floor. Fail-closed if the checker is
  unreachable; "ambiguity must not silently degrade to approval"; suppression = transform-to-
  labelled-false, not deletion.

### 2.4 Event-sourcing — events are the source of truth
- Store **only events**; a claim's confidence and a source's reputation are **folds over evidence
  events, never stored truth** — recomputed, auditable, replayable.
- When primary truth lands later, **replay and re-credit** each source as it stood *at the time*.
- The one append-only log is simultaneously the **audit trail**, the **RL trajectory substrate**, and
  the basis for every read-model projection. Snapshots are themselves events; one replay path.

---

## 3. Stack

| Concern | Choice |
|---|---|
| Host | **Hetzner**, EU-sovereign where feasible (cauri provisioning) |
| Kernel (deterministic spine) | **Rust** — append-only event log, NATS, the two clocks, mechanical tools (dedup, similarity, wire/copy detection, graph ops, scoring arithmetic, perceptual hashing) |
| Agent layer (LLM judgement) | **Python** — the judgement agents + sub-agent spawning, over the bus |
| Canonical store | **Postgres + pgvector** (read-model projection of the event log) |
| Event bus + durable log | **NATS (JetStream)** |
| Frontier reasoning | **Claude/Mistral split** — Mistral for bulk/near-mechanical, Claude for the hardest corroboration judgement (possibly language-aware). Claude allowed via an **EU region** (Bedrock/Vertex; route TBD). "European as much as possible" = strong preference, not an absolute line |
| Embeddings | **Mistral embeddings** (behind a swappable tool interface) |
| Vision (multimodal evidence) | Claude / Pixtral, + perceptual hashing in the kernel |
| Acquisition tools (MCP) | **SearXNG** (EU-sovereign meta-search) + Brave/Apify; pluggable |
| Observability + immediate-eval | **cat-cafe** (Apache-2.0, self-hosted EU): OTLP sink, trace UI, online LLM-judge, eval metrics |
| Client | **SwiftUI** universal app (iPhone + Mac), **iOS 27** beta; Foundation Models on-device |
| Telemetry | OpenTelemetry (GenAI semantic conventions) |

---

## 4. The veracity engine (the core)

Each article's **claims** pass through the pipeline (the claim, not the article, is the unit):

1. **Extract claims** — atomic assertions.
2. **Classify attribution vs assertion** — "X said Y" (outlet reports a truth even if Y is false)
   vs the embedded claim Y (scored on its own, dinging the speaker). Catch the three laundering
   abuses: endorsement, bare-repetition-as-fact (headline asserts), selective amplification
   (source-level, over time).
3. **Classify fact vs projection** — present-tense truth (→ reputation) vs forecast/judgement
   (→ accuracy, never reputation). Mind the grey zone ("will collapse" vs "is collapsing"); handle
   synthesis (outlet reasons to a new factual claim → it originates and owns it).
4. **Cluster claims** — tight identity (same fact), distinct from threading.
5. **Score corroboration by independent originators, not spread** — collapse wire syndication,
   citation cascades, and common-ownership/copy-networks to single nodes. Weight primary sources
   (named official, filed document, dataset, on-the-ground reporting) above any secondary repetition.
   **Reputation-weighted but bounded:** a higher-reputation source counts for more, but the weighting
   is bounded — never over-index on one source, and reputation shifts over time. An unknown/low-rep
   source backed by a **primary source** must still be able to win (protects §6.6 anti-conformity).
6. **Scale the bar to extremity** — each claim carries a prior; corroboration weight needed rises
   with distance from prior. Extraordinary claims, extraordinary evidence.
7. **Gate, then label** — suppress only below a genuinely low floor (Ammit); label everything above
   with a confidence read. **Never gate above the floor.** Story confidence rolls up from claims.

**Decided refinements:**
- **Judge in the native language**; never score a translation (it alters *said* vs *alleged*, hedges,
  attribution). Translate only for reader display — **on-device first, cloud-foundation-model
  fallback**.
- **Multimodality is first-class evidence**, but **suspect** (AI-generation). The independent-
  originator logic extends to media: the *same clip reshared* collapses to one source (perceptual-
  dedup, like wire syndication); *N independent angles/captures of one event* corroborate. Don't
  lean on deepfake detectors (weak prior at most) — rely on independent-capture corroboration. Media
  publishers/capturers are entities with reputation too.
- **Fetch-and-verify primary sources**; if the ultimate source can't be confirmed, make the
  judgement call at **lower confidence** rather than dropping the claim.

---

## 5. Learning & RL

- **RL is a core pillar.** The system learns *where to compensate for bias* from **truth resolving
  over time**. Reward is anchored to eventual **primary truth, never consensus** — the single most
  dangerous thing in the system; consensus-as-reward rebuilds the conformity machine.
- **Cold start → counter-prompting** (push hard against the model's Anglo-default prior), then
  **data-driven compensation** as resolved truth accrues (which sources/regions were right when
  consensus was wrong).
- **Backfill:** replay ~5 years of news (or a statistically significant window) and self-learn
  against primary truth to bootstrap reputation + bias-compensation. A **decaying, capped prior** —
  resolved-subset-only, with **archive-bias correction** (archives over-represent Anglo majors; a
  naive backfill would amplify the exact slant we're escaping).
- **The independent-originator / media-ownership graph is a learned, first-class asset** Maat builds
  via acquisition — not a dataset we buy (those are Anglo-rich, global-thin).
- **Safety frame = bounded self-modification:** agents grow *cognition* freely (author per-task
  trackers, validated at load) but *sources + scoring authority are kernel-granted capabilities they
  cannot self-escalate*. RL split: **learn the timing** (when to adapt), **validate the artifact**
  (what to change). Action space constrained to data, reward outside the kernel, every adaptation
  replayable.
- **Regression-safe by construction:** counterfactually **A/B a learned policy on replayed history**
  before it goes live; learned weights are versioned and rollback-able.

---

## 6. Data, multi-tenancy & privacy

- **Shared truth layer (all users):** the veracity engine, reputation, accuracy, the event-node
  graph, source-learning. Scale strengthens it.
- **Per-user:** topics (natural language), comments, pins, on-device personalisation, the feed view.
  Tenant-scoped from day one in the data model — no retrofit.
- **User-activity capture is collection-only right now.** Gather engagement signals (open→read-
  summary→leave, read-whole→comment, read-half→abandon, feedback taps, …); later analyse with
  regression to learn *what they signal*. We do **not** pre-decide what they mean or where they route.
  - **Two lanes:** individual signals **stay on-device** (per-user personalisation, never leave the
    phone); **aggregate, anonymised** signals (anonymised/aggregated **at the edge, on-device**,
    before transmission) feed the shared layer's learning. Edge-aggregation makes "aggregate,
    anonymised, outside GDPR/Apple" true by construction. Be cautious as we go; capture for a named
    purpose or don't capture it.
- **Comments/pins:** capture text + story + timestamp (per §10 of the brief — gather only for now);
  separately record which sources expose reader-comment mechanisms (for later upstream reposting).
- **Storage schema is deliberately not pre-designed** — capture raw articles, claims, entities,
  scores, and graph relations in the simplest durable store; let real data shape the model.

---

## 7. Quality & regression

Organised by **the bright line**: the two halves regress differently and get different regimes.

- **Deterministic kernel** (folds, scoring math, dedup, originator-collapse, graph ops): property
  tests that name their invariant + type-soundness + `clippy -D warnings`, **hard-block CI gate**;
  **replay-goldens** (same event log ⇒ same derived scores) from day one; add **reference-spec-as-
  oracle differential testing** once the scoring math stabilises.
- **Judgement rim** (LLM agents): **evals not tests** — golden adjudicated corpus → per-stage
  precision/recall + originator-collapse correctness + **calibration** (Brier / reliability);
  **advisory-band gating** (block on clear drops; noisy margin advisory; the longitudinal production
  metric is the real arbiter). Prompts + model versions are pinned/versioned, **eval-on-change**
  (prompt changes reviewed with cauri).
- **Two regression clocks:** *leading* = golden resolved-historical corpus at change-time; *lagging*
  = production calibration + hold-rate + the de-US-centering metric. Silent quality drift
  (miscalibration, re-slanting) is the real enemy — continuous production metrics are first-class
  regression defence.
- **RL regression:** counterfactual A/B on replayed history before go-live; reward/resolution logic
  guarded hardest and treated as untrusted-input-resistant.
- **Tests:** unit + integration coverage, **all on CI** — deterministic tests block the merge, LLM
  evals run advisory + nightly. Coverage is a ratcheting floor on deterministic code; the rim is
  measured by eval pass-rate/calibration, not line coverage.
- **Process from day one:** ADR-style `DECISIONS.md`, a `TIMELINE.md` of pivots, and the "what breaks
  if wrong?" review ritual on high-stakes invariants (reward, gate floor, scoring math).

---

## 8. Build sequence

**Principle: riskiest, highest-value slice first — prove the core, then grow outward.** Don't stand
the whole estate up at once. The cross-cutting **evaluation harness** and the bright-line quality
discipline start in Phase 1 and run forever.

- **P0 — Foundations (minimal, enabling).** Repo + process docs + CI skeleton. The smallest event-
  store spine (append-only log + fold/projection) on Postgres+pgvector. The Source/Effect seam with
  Claude + Mistral behind one interface (per-call model selectable) + Mistral embeddings. A small
  multilingual **golden corpus** (incl. a wire-syndication cluster, a laundering example, a
  fact/projection grey-zone pair, some resolved historical claims).
- **P1 — Veracity-core vertical slice (make-or-break).** The claim pipeline end-to-end on the corpus:
  extract → classify (attribution/assertion, fact/projection) → cluster → corroboration by
  independent originator → extremity-scaled confidence → gate-the-floor + label → story rollup.
  **Exit:** the wire cluster collapses to one originator and confidence reads are sane; the eval
  harness reports per-stage metrics + calibration; cat-cafe wired as OTLP sink + immediate judge.
- **P2 — Acquisition & ingestion loop.** Acquisition agent (broad web search via MCP, any language),
  learning loop narrowing toward rewarding sources; ingestion clock every few hours, incremental
  deltas only.
- **P3 — Entities, reputation, accuracy + RL/backfill.** Identity resolution; reputation as a time-
  trajectory fold (vs primary truth, never consensus); accuracy axis (dormant→resolve/extend/decay),
  computed not displayed; projection-harvester clock; backfill prior + archive-bias correction; the
  RL loop + the learned originator/ownership graph.
- **P4 — Story graph (threading).** Event-nodes + typed edges (develops/spawns/merges); claim↔node
  many-to-many; "you've seen this" on claim-novelty (collapses wire reprints for free).
- **P5 — Curation, multi-tenancy & serving.** Multi-tenant model; curation agent (de-US-centering an
  explicit objective); feed API serving stories+claims+labels+confidence; comments + pins; NL topics;
  auth.
- **P6 — Apple client (Tier 2 + Tier 3).** SwiftUI universal app; on-device re-rank / summarise /
  semantic search; on-device translation (default) + cloud fallback; comments local; Tier-3 "go
  deeper"; edge-aggregated analytics capture.
- **P7 — Feedback loop, de-slant validation & hardening.** Feedback intake → triage agent →
  auto-fix-PR / review-queue (guardrails below, to confirm); de-US-centering metrics; calibration-
  in-production; observability; red-team the laundering guards.

---

## 9. Borrowed & adopted

- **Gamelan — inspiration only, never a fork (IP boundary).** Patterns re-derived under Maat's own
  names, no bespoke/branded identifiers carried over. Borrowed: the Source/Effect seam, event-sourced
  folds, the Check/Verdict gate, bounded self-modification, working-memory-topology framing, the
  federation/trust patterns (for later multi-tenancy + the poisoned-evidence / "context-as-attack-
  surface" threat), and the verification practices (proof-as-oracle, MBT, DECISIONS/TIMELINE/the
  "what breaks if wrong?" ritual). **Skipped:** the Lean/Quint proof corpus, the in-process no-
  backpressure transport, the multi-language extraction pipeline.
- **cat-cafe — adopted (Apache-2.0, self-hosted EU)** for the observability + immediate-eval half;
  the **longitudinal half** (deferred primary-truth resolution, confidence calibration, RL feedback,
  tenancy) is built natively in the event-sourced kernel, linked by `trace_id`.

---

## 10. Deferred (documented now, build later)

- **Conversational interface** — user talks to the platform about a story, asks questions.
- **Rich per-story context UI** — confidence, where/how verified (provenance), original language,
  source reputation + independent-originator count, fact-vs-projection split, media AI-gen suspicion.
- **Graph visibility** — see how a story connects to others via the event-node graph.
- **Feedback auto-fix loop** — intake → triage → auto-fix-PR / review-queue.
- **On-device LoRA personalisation** (per-user).
- **Push notifications** (after the feed proves itself).
- **Accuracy display** (computed quietly from day one; surfaced later).

---

## 11. Open questions (resolved in-flight, not guessed now)

- **Claim→event-node attachment mechanism** — content alone, or content + named entities (and so
  which entities get spines). Spike in P4.
- **Storage schema** — kept thin; let real data shape it.
- **Prompts** — the veracity-stage prompts are product-critical; drafted *with cauri*, eval-gated.
- **Feedback auto-fix guardrails** — proposed, **to confirm**: "fix immediately" = open a green PR
  (not auto-deploy); the veracity core (reward, gate, scoring, prompts) is **never** auto-fixed;
  feedback is **untrusted input** (coordinated feedback = an attack vector).
- **What engagement signals actually indicate** — open empirical question for later regression; no
  pre-imposed routing.
- **Tier-3 PCC developer surface** and the **on-device↔server MCP direction** — verify at P6.
- **Task tracker** — `ba` vs GitHub issues (decide once the repo exists).

---

## 12. Prerequisites (cauri is setting up)

- **Hetzner** box · **GitHub** repo + CI · **Mistral** API key · the **Claude** route (Anthropic
  direct vs Bedrock/Vertex EU) · **Apple Developer** account on the iOS 27 beta (for P6).
- Add the original product spec as **`BRIEF.md`** (the source of truth for product *why*).
