# robotsix-board

Shared kanban-board frontend library: column-per-status board of cards with a move-between-columns action, auto-refresh, and a click-through detail panel. Owns the board HTML/CSS/JS chrome, parameterized by a small data adapter (column order, card fields, move endpoint) and a render mode (server-rendered fragments vs JSON+JS hydration). Consumed by robotsix-mill (FastAPI + static files) and robotsix-auto-mail (stdlib BaseHTTPRequestHandler + inline Jinja).

## Installation

From source via git+https (recommended until the first PyPI release):

```bash
pip install "robotsix-board @ git+https://github.com/damien-robotsix/robotsix-board.git"
```

Or add it to your consumer `pyproject.toml`:

```toml
dependencies = [
    "robotsix-board @ git+https://github.com/damien-robotsix/robotsix-board.git",
]
```

`pip install robotsix-board` (PyPI) will work once the package is published.

## Usage

The library owns the board HTML/CSS/JS chrome and is parameterized by two things:

- **A data adapter** that describes the board's shape — the column order, which card fields to display, and the endpoint used to move a card between columns.
- **A render mode** that selects how the board is produced — server-rendered HTML fragments, or a JSON payload hydrated by the bundled JavaScript.

This lets it be consumed by both robotsix-mill (FastAPI + static files) and robotsix-auto-mail (stdlib `BaseHTTPRequestHandler` + inline Jinja).

> **Note:** The public API is under active development and is not yet importable from the current package. The data-adapter and render-mode parameterization described above illustrates the planned usage; concrete imports, class names, and API reference docs will follow once the exported modules are in place.

## Development

Clone the repository, then install development dependencies:

```bash
uv sync --extra dev
```

Run tests:

```bash
uv run pytest
```

Lint and format with ruff:

```bash
uv run ruff check .
uv run ruff format .
```

Or run all pre-commit hooks at once:

```bash
uv run pre-commit run --all-files
```

CI runs `uv lock` → `uv sync --frozen --extra dev` → `uv run deptry .` → tests.

## Contributing

Open a focused PR against the default branch. Ensure ruff (lint + format) and tests pass and pre-commit hooks are clean before submitting. Code style is enforced by ruff.

## License

MIT — see the [LICENSE](LICENSE) file.

Copyright 2026 Damien Robotsix.
