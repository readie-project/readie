UV ?= uv

.PHONY: install fmt lint type test test-unit test-integration cover run docker clean help

install: ## Sync the virtualenv from the lockfile
	$(UV) sync --all-groups

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

test-unit: ## Unit tests only (no event loop, no sockets)
	$(UV) run pytest tests/unit

test-integration: ## Integration tests over a real in-process gRPC channel
	$(UV) run pytest tests/integration

cover: ## Run tests with a coverage report
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=html

run: ## Run the router from source
	$(UV) run python -m crfs_router

docker: ## Build the router image
	docker build -t crfs-router:latest .

clean: ## Remove build and test artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
