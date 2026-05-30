# Local development (no Docker). Override the interpreter with
# `make PYTHON=python3 install` if 3.14 isn't on PATH as python3.14.
PYTHON ?= python3.14
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: install test serve dev docker clean docs-serve docs-build

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)

# Editable install with dev (+tracing) extras into a local venv.
# Uses uv sync with the committed uv.lock for reproducible installs.
install:
	uv lock
	uv sync --frozen --group dev --extra tracing

test: install
	$(BIN)/python -m pytest -q --cov=robotsix_mill --cov-report=term-missing --cov-fail-under=70

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
	$(BIN)/pip install -q -e '.[docs]'
	$(BIN)/mkdocs serve

# Build static site into site/
docs-build: install
	$(BIN)/pip install -q -e '.[docs]'
	$(BIN)/mkdocs build

clean:
	rm -rf $(VENV) .mill-data .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
