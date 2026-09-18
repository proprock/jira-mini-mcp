# Convenience wrapper around QUALITY.md's commands -- it is not a second
# source of truth for what "passing" means. Keep targets in sync with
# QUALITY.md whenever either changes.
#
# macOS/Linux (and CI, which runs on Linux runners) only. This repo's dev
# machine is Windows without GNU Make on PATH by default, and Windows is not
# a target for this convenience wrapper -- Windows contributors run
# QUALITY.md's commands directly instead of through `make`.

.DEFAULT_GOAL := help

.PHONY: help sync hooks format format-check lint typecheck test test-all coverage check all clean

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-14s %s\n", $$1, $$2}'

sync: ## Install/sync dependencies
	uv sync

hooks: ## Install the prek git hooks
	uv run prek install

format: ## Apply ruff formatting
	uv run ruff format .

format-check: ## Check formatting without applying it
	uv run ruff format --check .

lint: ## Run ruff checks
	uv run ruff check .

typecheck: ## Run ty type checking
	uv run ty check

test: ## Run the pytest suite
	uv run pytest

test-all: ## Run every test, including hygiene and slow ones
	uv run pytest -o addopts=""

coverage: ## Run the full suite with statement and branch coverage
	uv run pytest -o addopts="" --cov=jira_mini_mcp --cov-branch --cov-report=term-missing

check: sync format-check lint typecheck test coverage ## Run the full Definition of done sequence
all: check ## Alias for check

clean: ## Remove local, gitignored tool caches and coverage output
	rm -rf .pytest_cache/ .ruff_cache/ .coverage htmlcov/
