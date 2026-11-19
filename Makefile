# Every dev command for the workspace. CI runs these same targets, so local and CI agree.
# `make help` lists the targets. Override PY (interpreter that creates the venv) or VENV.

PY ?= python3
VENV ?= .venv
PYTHON := $(VENV)/bin/python
BIN := $(VENV)/bin

# Packages in dependency order; ones that do not exist yet are skipped.
PACKAGE_ORDER := common dictionaries record labels desk engine dojo launcher
PACKAGES := $(foreach p,$(PACKAGE_ORDER),$(if $(wildcard packages/$(p)/pyproject.toml),$(p)))
# Extras installed per package (default: dev).
EXTRAS_record := dev,analysis
INSTALL_SPECS := $(foreach p,$(PACKAGES),packages/$(p)[$(or $(EXTRAS_$(p)),dev)])

# Packages under mypy --strict (see [tool.mypy] in pyproject.toml; record is a follow-up).
TYPED_PACKAGES := $(foreach p,common dictionaries,$(if $(wildcard packages/$(p)/src),$(p)))

# Dev tools, pinned to the versions .pre-commit-config.yaml uses.
DEV_TOOLS := ruff==0.17.5 mypy==2.4.1 codespell==2.4.3 pre-commit==4.7.0 types-PyYAML pytest-xdist

.DEFAULT_GOAL := help
.PHONY: help install test test-fast lint format typecheck spell boundaries layout session-links check hooks

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

$(PYTHON):
	$(PY) -m venv $(VENV)
	$(PYTHON) -m pip install -q --upgrade pip

install: $(PYTHON) ## Create .venv; install every package (dependency order, dev extras) and the dev tools
	@set -ef; for spec in $(INSTALL_SPECS); do \
		echo "pip install -e $$spec"; $(PYTHON) -m pip install -q -e "$$spec"; \
	done
	$(PYTHON) -m pip install -q $(DEV_TOOLS)

test: ## Run every package's tests and the workspace tests
	$(PYTHON) -m pytest -q

test-fast: ## Same, stopping at the first failure, in parallel
	$(PYTHON) -m pytest -x -q -n auto

lint: ## ruff check and ruff format --check (no changes made)
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Apply ruff's safe fixes and formatting
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck: ## mypy --strict, one run per package so same-named test files do not collide
	@set -e; for p in $(TYPED_PACKAGES); do \
		targets="packages/$$p/src"; \
		if [ -d "packages/$$p/tests" ]; then targets="$$targets packages/$$p/tests"; fi; \
		echo "mypy $$targets"; $(BIN)/mypy $$targets; \
	done
	$(BIN)/mypy scripts tests

spell: ## codespell over the tracked text (config in pyproject.toml)
	$(BIN)/codespell

boundaries: ## The one-way dependency rule between packages
	$(PYTHON) scripts/check_boundaries.py

layout: ## README.md's layout map against the real folders
	$(PYTHON) scripts/check_layout.py

session-links: ## No session links in tracked files or in commit messages since origin/main (skipped if absent)
	bash scripts/check_no_session_links.sh

check: lint typecheck spell boundaries layout session-links test ## Every gate, in CI order

hooks: ## Install the pre-commit and commit-msg hooks
	$(BIN)/pre-commit install
