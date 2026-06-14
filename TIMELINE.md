# Maat — Timeline

A running narrative log of what happened and *why we changed course* — including dead-ends and
reversals (normal git history loses the "why we abandoned X"). One entry per meaningful day/decision
point. Newest at top.

---

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
