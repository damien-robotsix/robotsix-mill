# Local development (no Docker). Override the interpreter with
# `make PYTHON=python3 install` if 3.14 isn't on PATH as python3.14.
PYTHON ?= python3.14
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: install test serve dev docker clean

$(BIN)/activate:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip

# Editable install with dev (+tracing) extras into a local venv.
install: $(BIN)/activate
	$(BIN)/pip install -q -e ".[dev,tracing]"

test: install
	$(BIN)/python -m pytest -q

# Run the service as it runs in prod/Docker (reads ./.env, data in
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

clean:
	rm -rf $(VENV) .mill-data .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
