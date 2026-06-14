# Golden corpus

The adjudicated multilingual corpus that drives **both** the eval harness (PLAN §7) and the RL
backfill (PLAN §5). Built deliberately to include the hard cases:

- a **wire-syndication cluster** (one AFP/Reuters/AP story reprinted near-verbatim) — must collapse
  to a single originator;
- each **attribution-laundering** abuse (endorsement, bare-repetition-as-fact, selective
  amplification);
- the **fact-vs-projection grey-zone** pair ("the deal will collapse" vs "the deal is collapsing");
- **resolved historical claims** (with an answer key) — the leading-indicator eval signal and the
  backfill anchor;
- **any language / culture** (the EN/PT/FR constraint was dropped — DECISIONS D10).

Structure TBD with the first real data (storage schema is deliberately not pre-designed — PLAN §11).
Nothing here yet; seeded in P1.
