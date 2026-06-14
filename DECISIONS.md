# Maat — Decision Record

ADR-style log of the engineering decisions made so far. Format per decision: **Decision** ·
**Context** · **Options** · **Why** (and, where it matters, who decided). Newest context lives in
`PLAN.md`; this file is the *why* behind each call so we don't re-litigate. The product *why* lives in
`BRIEF.md`.

_Started 2026-06-14 (design conversation)._

---

### D1 — Name: Maat
**Decision:** the product/repo/CLI is **Maat** (Ma'at, "mah-AHT"), no apostrophe. Agents named from
the myth: Anubis (scorer), Thoth (scribe/store), Assessor (classification judgement), Ammit (gate).
**Why:** Ma'at = truth/balance, the heart weighed against the feather — the product's literal
mechanic, not a skin. No apostrophe keeps URLs/packages/DNS clean.

### D2 — No orchestrator; peer-to-peer choreography
**Decision:** agents coordinate peer-to-peer (event choreography + backward "find me corroboration"
asks). No central orchestrator (no "Osiris"). **A2A** the protocol deferred until a real cross-trust
boundary exists. **Options:** A2A mesh + orchestrator now / logical agents + simple coordination.
**Why (cauri):** getting agents to coordinate beats a conductor; A2A ceremony buys nothing inside one
backend yet.

### D3 — Agent decomposition: judgement → agent, mechanical → tool; dynamic sub-agents
**Decision:** judgement work is an agent; deterministic work is a tool. Any agent can **spawn
sub-agents on demand** — decomposition is a runtime decision, not a fixed roster. **Why (cauri):**
keeps the agent count honest and emergent; "tools become agents only when they must judge" (a lean,
not a law).

### D4 — Rust kernel + Python multi-agent layer
**Decision:** Rust for the deterministic kernel/spine; Python for the LLM judgement agents.
**Options:** all-Rust / Rust kernel + Python agents. **Why (cauri):** Rust gives a fast, correct,
auditable spine; Python keeps the agents where the LLM/agent/MCP ecosystem is richest.

### D5 — Event-sourcing from the start; events are the source of truth
**Decision:** append-only event log is the source of truth; confidence/reputation are folds over
events, never stored truth; one log = audit + RL substrate + projections. **Options:** event-sourcing
on the spine / events as an audit layer over a mutable store. **Why (cauri):** auditability +
longitudinal re-credit against primary truth fall out for free; it's the substrate the whole product
needs.

### D6 — Postgres + pgvector; NATS (JetStream)
**Decision:** Postgres+pgvector as the read-model/canonical store; NATS JetStream as the durable
event log + bus. **Why:** simplest durable store that also carries the graph + vectors; NATS does both
the choreography and the durable audit log; both self-hostable EU.

### D7 — Claude/Mistral split; Claude allowed via EU region
**Decision:** Mistral for bulk/near-mechanical stages, Claude for the hardest corroboration judgement
(possibly language-aware); route Claude via an EU region. **Options:** Claude-in-region / Mistral-only
/ split. **Why (cauri):** "European as much as possible" is a strong preference, **not** a hard line;
the core judgement gets the strongest model.

### D8 — Mistral embeddings
**Decision:** use Mistral embeddings behind a swappable tool interface. **Why (cauri):** worth trying;
trivially swappable; watch language coverage once any-language. 

### D9 — Host on Hetzner, EU-sovereign where feasible
**Decision:** self-host on Hetzner in the EU; European infra as much as possible. **Why (cauri):**
sovereignty. Honest limit: Apple on-device + PCC (Tier 2/3) are inherently Apple/US.

### D10 — Judge in native language; translate for display only
**Decision:** veracity judgement runs in the source language; translation is display-only, on-device
first with cloud-foundation-model fallback. **Why:** translation alters the things veracity hinges on
(said vs alleged, hedges, attribution) — never score a translation. EN/PT/FR constraint **dropped**:
any language/culture works.

### D11 — RL pillar; reward = primary truth, never consensus; ~5y backfill
**Decision:** RL learns where to compensate for bias from truth-over-time; reward anchored to eventual
primary truth, never consensus; bootstrap by replaying ~5 years (decaying capped prior, resolved-
subset-only, archive-bias corrected). **Why (cauri):** truth-over-time tells us where bias is;
consensus-as-reward would rebuild the conformity machine.

### D12 — Corroboration is reputation-weighted but bounded
**Decision:** higher-reputation sources count for more toward corroboration, but bounded — never
over-index on one source; reputation shifts over time; a primary source lets an unknown/low-rep source
still win. **Why (cauri):** weight reputation without rebuilding conformity (protects §6.6).

### D13 — Multimodality is first-class, suspect evidence
**Decision:** images/video are evidence but AI-gen-suspect; corroboration carries them. Same clip
reshared = one source (perceptual-dedup); N independent angles corroborate. Don't trust deepfake
detectors over corroboration. **Why (cauri):** corroboration is the real defence against synthetic
media; mirrors the text independent-originator logic.

### D14 — Gamelan = inspiration, never a fork (IP)
**Decision:** Gamelan may inspire Maat's platform (Source/Effect seam, event-sourced folds, Check/
Verdict gate, bounded self-modification, verification practices) but is never referenced or forked;
patterns re-derived under Maat's own names. **Why (cauri):** keep Maat's IP lineage clean.

### D15 — cat-cafe adopted for observability + immediate-eval; longitudinal eval is native
**Decision:** adopt cat-cafe (Apache-2.0, self-hosted EU) for trace ingestion, trace UI, online LLM-
judge, eval metrics; build the longitudinal half (deferred primary-truth resolution, calibration, RL
feedback, tenancy) natively in the event-sourced kernel, linked by `trace_id`. **Why:** cat-cafe's
data model (7-day TTL, resolve-at-trace-time, no tenancy) fights the longitudinal loop — which our
event log owns by design.

### D16 — Code quality organised by the bright line
**Decision:** deterministic kernel = property tests + replay-goldens + clippy-deny, **hard-block**;
reference-oracle differential testing added once scoring stabilises. Judgement rim = evals +
calibration, **advisory-band** gating. Unit + integration coverage, **all on CI** (deterministic
blocks, evals advisory + nightly). DECISIONS/TIMELINE + "what breaks if wrong?" ritual from day one.
**Why (cauri):** advisory band on the rim, hard block on the kernel; #3 (oracle timing) delegated to
me — oracle pays off once there's a stable thing to be an oracle of.

### D17 — User-activity capture: collection-only, edge-aggregated, two lanes
**Decision:** capture engagement signals for the platform's learning; **collection-only now**,
meaning discovered later by regression. Individual signals stay on-device (personalisation); aggregate
signals are anonymised **at the edge** before transmission and feed the shared layer. **What the
signals mean is an open question — no pre-imposed routing.** **Why (cauri):** edge-aggregation makes
"aggregate/anonymised" GDPR/Apple-safe by construction; don't govern data we haven't collected.

### D18 — Feedback auto-fix loop (design; guardrails TO CONFIRM)
**Decision (shape):** in-app feedback → event → triage agent → doable: coding sub-agent opens a
tested PR through CI; questionable: review-queue projection. Intake/triage/queue bespoke on the event
log (EU-sovereign); code path via real PRs. **Guardrails proposed, awaiting cauri:** "fix immediately"
= green PR not auto-deploy; veracity core never auto-fixed; feedback is untrusted input.

### D19 — Build riskiest-slice-first
**Decision:** start with the veracity-core vertical slice on a small corpus (P1), not the whole
estate; deferred items (schema, claim→node attachment, prompts) resolve by building. **Why:** the core
is the make-or-break; the brief itself says expect change as the build teaches us.

### D20 — Stand up the real event-sourced architecture from the start
**Decision (cauri):** build the real system, not a Python prototype, and iterate in it. **Shape:**
Postgres = append-only `events` log (source of truth) + projections + pgvector; NATS = live
choreography bus; the Rust kernel (`maat-kerneld`) = the deterministic spine / single writer
(validates + appends events, folds projections, will host the mechanical tools); Python agents = LLM
judgement on the bus. Refines D6 — Postgres is the durable event store, NATS is transport, not the
durable log; a pragmatic iteration cauri sanctioned ("iterate in the real thing").

### D21 — Single-user now; tenant-aware but not over-engineered
**Decision (cauri):** "this is for me only for right now." Schema carries a `tenant_id` (default
`cauri`) so multi-tenancy isn't painted out, but build NO auth/tenant-isolation machinery yet.

### D22 — Autonomous-session flow + budget
**Decision (cauri delegated the flow):** one feature branch per coherent chunk → CI + Claude review →
self-merge when green → deploy on merge; no stacking (merge each before the next). Budget ≤ $1000 for
the session. Veracity prompts created without per-prompt sign-off, each marked `DRAFT — review on
return`.

### D23 — P6 Apple client: in-monorepo, fixture-first, Swift-5 mode, iOS 26 floor
**Decision (cauri):** build the SwiftUI universal client (iPhone + Mac) **in this monorepo** under
`apple/` — not a separate repo — so the JSON feed contract and the Swift models are reviewed together
and can't drift, and the client reuses the `corpus/` fixtures. iOS CI rides along as a path-filtered
workflow (`apple/**`). **Data:** the client reads a JSON feed API **stubbed on the reader** (P5 #48
minimal — `/api/feed`, `/api/story/{id}?deeper=1`, `/api/translate`) over the same projections the HTML
view uses; a bundled corpus-derived fixture (`feed.fixture.json`) lets it build/preview/run with no
backend. **Floor:** deployment target **iOS/macOS 26.0** (Foundation Models, Translation, SwiftData all
land at 26), built against the **iOS 27 SDK** (Xcode 27) — a 26 floor lets the same source compile on
the stable 26.5 toolchain *and* widens device reach without giving up anything P6 needs. **Tier-2/3
shape:** on-device re-rank / summarise / semantic search (Foundation Models + NaturalLanguage), on-
device translation (Apple Translation framework) with cloud→identity fallback, local-only comments
(SwiftData), Tier-3 "go deeper" (server/PCC boundary stubbed), edge-aggregated analytics (two lanes,
collection-only). Every on-device path sits behind a protocol with a deterministic fallback + an
availability check, so it builds and runs where Apple Intelligence is off (e.g. the simulator).
**Why Swift 5 language mode (target-wide, temporary):** the Translation framework's `TranslationSession`
(non-`Sendable`, `@concurrent translate`) can't be driven from the main-actor `.translationTask`
closure under Swift 6 strict concurrency on this SDK; the rest of the app is Swift-6-clean. Revisit when
the API's isolation annotations are ergonomic (candidate: isolate the Translation glue into its own
small module). **Prompts:** the on-device `Summarizer` / `Reranker` instructions fed to Foundation
Models are **`DRAFT — review with cauri`** (per D22; they are in-platform agent prompts). Verified
building on macOS + iOS 27 SDK and running on the iOS 27 simulator across all six P6 stories.
