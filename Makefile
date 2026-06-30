# Local development (no Docker). Override the interpreter with
# `make PYTHON=python3 install` if 3.14 isn't on PATH as python3.14.
PYTHON ?= python3.14
VENV   := .venv
BIN    := $(VENV)/bin

SOURCES ?= src/ tests/ scripts/ vulture_whitelist.py deploy/split_config.py dev/

.PHONY: install test format lint serve dev docker clean docs-serve docs-build

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)

# Editable install with the dev group (+tracing extra) into a local venv.
# Uses uv sync with the committed uv.lock for reproducible installs.
install:
	uv sync --frozen --extra tracing

test: install
	$(BIN)/python -m pytest -q --cov=robotsix_mill --cov-report=term-missing --cov-fail-under=70

.PHONY: format  ## Auto-format Python source files
format: install
	uv run ruff check --fix $(SOURCES)
	uv run ruff format $(SOURCES)

.PHONY: lint  ## Lint Python source files (check only)
lint: install
	uv run ruff check $(SOURCES)
	uv run ruff format --check $(SOURCES)
	uv run mypy src/ --strict

# Run the service as it runs in prod/Docker (reads ./.env and ./secrets.env, data in
# ./.mill-data). Ctrl-C to stop.
serve: install
	$(BIN)/robotsix-mill serve

# Same service with hot-reload for development.
dev: install
	$(BIN)/uvicorn --factory robotsix_mill.runtime.api:create_app \
		--reload --reload-dir src --host 127.0.0.1 --port 8077

# Build + run the container instead.
docker:
	docker compose up -d --build

# Live-preview docs at http://127.0.0.1:8000
docs-serve: install
	uv sync --frozen --group docs
	$(BIN)/mkdocs serve

# Build static site into site/
docs-build: install
	uv sync --frozen --group docs
	$(BIN)/mkdocs build

clean:
	rm -rf $(VENV) .mill-data .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
