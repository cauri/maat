# Maat

**Maat** (Ma'at, "mah-AHT") — a multi-tenant, veracity-weighted personal news feed that scores
**corroboration over spread**, attaches a **confidence read** to every story, and pushes hard against
Anglo-American slant. The **claim**, not the article, is the unit.

- **What & why:** [`BRIEF.md`](BRIEF.md) — product source of truth (to be added)
- **Build plan:** [`PLAN.md`](PLAN.md)
- **Decisions (ADR log):** [`DECISIONS.md`](DECISIONS.md)
- **Pivots / reversals:** [`TIMELINE.md`](TIMELINE.md)

## Layout
- `rust/` — the deterministic **kernel** (event log, folds, mechanical tools). Cargo workspace.
- `python/` — the **agent layer** (LLM judgement agents) + the provider seam. uv project.
- `corpus/` — the golden adjudicated corpus (evals + RL backfill).
- `docker-compose.yml` — local Postgres + pgvector.
- `.github/workflows/ci.yml` — deterministic CI gates (rust + python).

## Dev quickstart
```sh
cp .env.example .env      # then fill in keys (gitignored)
make db-up                # local Postgres + pgvector
make kernel-test          # rust kernel tests
make py-setup             # python env (uv)
make py-smoke             # verify Claude + Mistral keys (hits live APIs)
```

## Notes
- **Secrets** live only in `.env` (gitignored). Never commit keys.
- The local checkout folder may be named `news`; the repo is `maat`. Rename locally if you like.
- Status: **P0 foundations** (PLAN §8). Provider keys verified working 2026-06-14.
