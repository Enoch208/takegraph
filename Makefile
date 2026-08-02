# Root task runner (PRD §7.2). Every target is noninteractive and returns
# nonzero on failure so CI can use them directly.
.DEFAULT_GOAL := help
SHELL := /bin/bash
export DATABASE_URL ?= postgresql+asyncpg://takegraph:takegraph_local@127.0.0.1:5434/takegraph
export REDIS_URL ?= redis://127.0.0.1:6380/0

.PHONY: help setup up down dev migrate check test lint fmt typecheck doctor clean

help: ## Show available targets
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install locked dependencies for both toolchains
	uv sync
	pnpm install --frozen-lockfile

up: ## Start PostgreSQL and Redis
	docker compose up -d --wait

down: ## Stop local dependencies
	docker compose down

dev: up migrate ## Start API, worker deps and web
	@echo "API  -> http://127.0.0.1:8000/api/docs"
	@echo "web  -> http://localhost:3000"
	@( uv run uvicorn takegraph_api.main:app --reload --port 8000 & \
	   pnpm --filter @takegraph/web dev & \
	   wait )

migrate: ## Apply database migrations
	uv run alembic upgrade head

check: lint typecheck test ## Format check, lint, types and tests

lint: ## Ruff lint and format check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Apply formatting
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## Strict type checking, both toolchains
	uv run mypy packages/domain/takegraph_domain packages/infrastructure/takegraph_infrastructure services/api/takegraph_api services/worker/takegraph_worker scripts/doctor.py
	pnpm --filter @takegraph/web typecheck

test: ## Unit and integration tests
	uv run pytest -q

doctor: ## Validate env, DB, Redis, FFmpeg and provider readiness
	uv run python scripts/doctor.py

clean: ## Remove build artefacts
	rm -rf apps/web/.next .pytest_cache .ruff_cache .mypy_cache
