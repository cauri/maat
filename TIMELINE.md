# Maat — Timeline

A running narrative log of what happened and *why we changed course* — including dead-ends and
reversals (normal git history loses the "why we abandoned X"). One entry per meaningful day/decision
point. Newest at top.

---

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
