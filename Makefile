.PHONY: help kernel-test kernel-lint py-setup py-smoke py-lint db-up db-down ci

help:
	@echo "kernel-test  - cargo test (rust kernel)"
	@echo "kernel-lint  - cargo clippy -D warnings"
	@echo "py-setup     - uv sync (python env, dev extras)"
	@echo "py-smoke     - verify Claude + Mistral keys (live APIs, costs \$$)"
	@echo "py-lint      - ruff check"
	@echo "db-up/db-down- local Postgres + pgvector"
	@echo "ci           - deterministic gates (kernel-test, kernel-lint, py-lint)"

kernel-test:
	cd rust && cargo test --all

kernel-lint:
	cd rust && cargo clippy --all-targets -- -D warnings

py-setup:
	cd python && uv sync --extra dev

py-smoke:
	cd python && uv run python scripts/smoke_providers.py

py-lint:
	cd python && uv run ruff check .

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

ci: kernel-test kernel-lint py-lint
