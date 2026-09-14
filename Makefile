# motionless — development tasks.
#
# PyGObject cannot be built reliably by pip, so the virtualenv is created with
# --system-site-packages and uses the distribution's GTK bindings.

VENV    ?= .venv
PYTHON  ?= python3
BIN      = $(VENV)/bin

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(BIN)/python:
	$(PYTHON) -m venv --system-site-packages $(VENV)
	$(BIN)/pip install --quiet --upgrade pip

.PHONY: dev
dev: $(BIN)/python ## Create the virtualenv and install in editable mode
	$(BIN)/pip install -e '.[dev]'
	@echo "Activate with: source $(VENV)/bin/activate"

.PHONY: check
check: lint typecheck test ## Everything CI runs

.PHONY: lint
lint: ## ruff check and format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

.PHONY: format
format: ## Apply ruff formatting and fixes
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

.PHONY: typecheck
typecheck: ## mypy
	$(BIN)/mypy

.PHONY: test
test: ## pytest with coverage
	$(BIN)/pytest --cov=motionless --cov-report=term-missing

.PHONY: run
run: ## Run the overlay against the synthetic drive loop
	$(BIN)/motionless run --source demo --always-on --verbose

.PHONY: screenshot
screenshot: ## Regenerate docs/screenshot.png by capturing the real overlay
	./docs/capture_screenshot.sh $(BIN)/motionless

.PHONY: build
build: ## Build the sdist and wheel
	$(BIN)/pip install --quiet build
	$(BIN)/python -m build

.PHONY: clean
clean: ## Remove build and cache artefacts
	rm -rf build dist ./*.egg-info src/*.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
