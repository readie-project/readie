UV ?= uv

.PHONY: install fmt lint type test cover clean help

install: ## Sync the virtualenv from the lockfile
	$(UV) sync --all-groups --all-extras

fmt: ## Format and apply safe lint fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

lint: ## Lint without modifying anything
	$(UV) run ruff format --check .
	$(UV) run ruff check .

type: ## Type-check with mypy --strict
	$(UV) run mypy

test: ## Run the whole suite
	$(UV) run pytest

cover: ## Run tests with a coverage report
	$(UV) run pytest --cov --cov-report=term-missing

clean: ## Remove build and test artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
