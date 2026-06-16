.PHONY: help kernel-test kernel-lint py-setup py-smoke py-lint eval eval-prompt acquire tick obs-up db-up db-down ci

help:
	@echo "kernel-test  - cargo test (rust kernel)"
	@echo "kernel-lint  - cargo clippy -D warnings"
	@echo "py-setup     - uv sync (python env, dev extras)"
	@echo "py-smoke     - verify Claude + Mistral keys (live APIs, costs \$$)"
	@echo "py-lint      - ruff check"
	@echo "eval         - eval harness over the projections (golden + metrics)"
	@echo "eval-prompt  - run the golden corpus with the ACTIVE prompts + score (live APIs, costs \$$)"
	@echo "acquire      - GDELT: fetch real articles (QUERY=... N=12, live web + APIs)"
	@echo "tick         - ingestion clock: acquire NEW articles for config/topics.txt"
	@echo "obs-up       - start cat-cafe (OTLP eval sink) on :8800 / :4318"
	@echo "db-up/db-down- local Postgres + pgvector"
	@echo "ci           - deterministic gates (kernel-test, kernel-lint, py-lint)"

kernel-test:
	cd rust && cargo test --all

kernel-lint:
	cd rust && cargo clippy --all-targets -- -D warnings

py-setup:
	cd python && uv sync --extra dev --extra obs

py-smoke:
	cd python && uv run python scripts/smoke_providers.py

agents:
	cd python && uv run python -m maat.agents

ingest-corpus:
	cd python && uv run python scripts/ingest_corpus.py

corroborate:
	cd python && uv run python -m maat.agents.corroborate_agent

eval:
	cd python && uv run python scripts/eval.py

eval-prompt:
	cd python && uv run python scripts/eval_prompt.py

acquire:
	cd python && uv run python scripts/acquire.py "$(QUERY)" $(N)

tick:
	cd python && uv run python scripts/clock.py

obs-up:
	docker compose --profile obs up -d cat-cafe
	@echo "cat-cafe UI http://localhost:8800 · OTLP http://localhost:4318"
	@echo "trace LLM calls: run agents with OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318"

web:
	cd python && uv run uvicorn maat.web.app:app --host 0.0.0.0 --port 8000

py-lint:
	cd python && uv run ruff check .

db-up:
	docker compose up -d postgres

db-down:
	docker compose down

ci: kernel-test kernel-lint py-lint
