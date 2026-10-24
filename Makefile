# ForgeFlow developer commands. Every target is a thin wrapper over `uv` and the
# `forgeflow` CLI so there is one obvious way to do each thing.
#
# Run `make` or `make help` to list the targets.

.DEFAULT_GOAL := help
.PHONY: help setup test lint format check discover rank report eval clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create the environment and install dependencies
	uv sync

test: ## Run the full test suite
	uv run python -m pytest -q

lint: ## Run ruff check
	uv run python -m ruff check .

format: ## Apply ruff formatting
	uv run python -m ruff format .

check: ## Run the same gates CI runs (lint, format check, tests)
	uv run python -m ruff check .
	uv run python -m ruff format --check .
	uv run python -m pytest -q

discover: ## Discover open issues from the allowlist (read-only; needs GITHUB_TOKEN)
	uv run forgeflow discover

rank: ## Rank stored issues with the six weighted factors
	uv run forgeflow rank

report: ## Write a Markdown report of ranked and excluded issues
	uv run forgeflow report

eval: ## Run the SPEC section 11 evaluation harness (offline)
	uv run python -m evaluation

clean: ## Remove local caches and the local database
	rm -rf .pytest_cache .ruff_cache
	rm -f data/forgeflow.db
