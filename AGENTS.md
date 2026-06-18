# AGENTS.md — working in this repo

Maat — a veracity-weighted news feed. Architecture + decisions: `PLAN.md`, `DECISIONS.md`.
Backlog: GitHub issues (epics P1–P13; no milestone on epics). (`ba`/`.ba/` is not used in this repo.)

**Operator console (rebuild):** being rebuilt from scratch under `console/` — Next.js + shadcn/ui with the
**Sia** collaborator — replacing `python/maat/web/app.py`. Start at **`console/README.md`** (epic #302,
decision D33); put new console work in `console/`, not `app.py`.

## Multi-agent coordination

Several agents work this repo **in parallel**. We've collided (a reader deploy broke once from a
config gap). Until the Agent Mail MCP is connected (see below), follow this convention:

1. **One agent per epic.** Before starting, comment on the epic/story issue ("picking up #N") and
   check it isn't already claimed or in an open PR. **Mark stories done as you go** (`gh issue close`).
2. **Check before editing hot, shared files.** These are touched by many: `python/maat/pipeline/corroborate.py`,
   `python/maat/web/app.py`, `rust/crates/maat-kerneld/src/main.rs`, `python/pyproject.toml`,
   `Makefile`, the compose files, and `migrations/`. Run `git fetch && git log origin/main --oneline -10`
   and `gh pr list` first; prefer adding new modules over editing shared ones.
3. **Rebase on `origin/main` before pushing.** Keep PRs small, resolve conflicts, merge promptly —
   a long-lived branch against this fast-moving main will conflict.
   - **Run the CI checks locally first** — `ruff check` + `pytest` (python), and for any Rust change
     `cargo clippy --all-targets -- -D warnings` + `cargo test`. CI fails the PR on clippy warnings, not
     just test failures, so `cargo test` alone is not enough. Don't `--admin`-merge past a red run.
4. **Migrations:** take the next free number (`ls rust/crates/maat-kerneld/migrations/`); never reuse one.
5. **Shared deploy:** merge to `main` push-deploys to the box. Don't break `deploy/docker-compose.prod.yml`
   (the reader needs `NATS_URL`); verify the box (`curl http://167.233.109.64:8000`) after a deploy.
6. **Veracity-stage prompts** (extraction, classification, extremity) are **co-designed with cauri** —
   flag changes for review, don't land them silently.

## Agent Mail (intended coordination layer — not yet connected)

CLAUDE.md describes MCP Agent Mail: identities, inbox/outbox, and advisory **file reservations** that
prevent exactly the collisions above. To enable it, connect its MCP server (project `.mcp.json` or
user settings), then `register_agent` per session and `file_reservation_paths(...)` before editing.
Use the GitHub issue id as the Mail `thread_id`.
