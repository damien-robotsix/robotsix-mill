# Local development (no Docker). Override the interpreter with
# `make PYTHON=python3 install` if 3.14 isn't on PATH as python3.14.
PYTHON ?= python3.14
VENV   := .venv
BIN    := $(VENV)/bin

.PHONY: install test serve dev docker clean

$(BIN)/activate:
	uv sync --frozen

install: $(BIN)/activate

test: install
	uv run --frozen pytest -q

# Run the service as it runs in prod/Docker (reads ./.env, data in
# ./.mill-data). Ctrl-C to stop.
serve: install
	uv run --frozen robotsix-mill serve

# Same service with hot-reload for development.
dev: install
	uv run --frozen uvicorn --factory robotsix_mill.runtime.api:create_app \
		--reload --reload-dir src --host 127.0.0.1 --port 8077

# Build + run the container instead.
docker:
	docker compose up -d --build

clean:
	rm -rf $(VENV) .mill-data .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
