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

## Design contract

This section documents the interface the follow-on build-out and the two
consumer-migration tickets target. It is the source of truth for the shape of
the board; the library implementation must not drift from it.

### Data adapter contract

A consumer drives the board by supplying an adapter (see
`robotsix_board.BoardAdapter`) that answers:

* **Column order + labels** — the ordered list of `(status_key, label)`
  pairs. Column order is significant: it is the left-to-right order columns
  appear on the board, and the labels are the human-readable column headings.
* **Card-field accessors** — given a card object, the adapter exposes:
  * `id` — a stable identifier (used as the DOM/card key and in move URLs).
  * `title` — the display title.
  * `badges` — zero or more short badge labels.
  * `timestamps` — named timestamp fields (e.g. created / updated).
* **Move endpoint** — the `(url, http_method)` used to move a card from one
  column to another. The move control posts the target `status_key` to this
  endpoint.

### Render-mode switch

The board supports two transports, selected via `robotsix_board.RenderMode`:

* **Server-rendered HTML fragments** (`RenderMode.SERVER_FRAGMENTS`) — the
  server emits ready-to-insert HTML fragments. This is the stdlib/Jinja
  consumer path (e.g. robotsix-auto-mail, `BaseHTTPRequestHandler` + inline
  Jinja).
* **JSON + client-side JS hydration** (`RenderMode.JSON_HYDRATION`) — the
  server emits JSON and the bundled `board.js` hydrates the markup on the
  client. This is the FastAPI consumer path (e.g. robotsix-mill, which mounts
  the packaged `static/` directory and serves `board.js`).

Both transports render the **same markup contract** so the styling and
behavior are shared regardless of transport.

### Shared markup contract

Both transports produce the same DOM shape:

* **Column container** — one container per column, keyed by `status_key`,
  headed by the column `label`.
* **Card markup** — a card element keyed by the card `id`, showing `title`,
  `badges`, and `timestamps`.
* **Move control** — a per-card form/dropdown listing the other columns;
  selecting a target column issues the adapter's move endpoint request.

All HTML interpolation goes through a single centralized `esc()` escaping
helper, so the server-fragment and JSON-hydration transports share **one**
escaping implementation rather than each re-implementing escaping.

### Static assets

The packaged `static/` directory ships `board.css` and `board.js` as package
data. Resolve it at runtime via `robotsix_board.static_dir()`:

* A FastAPI consumer mounts the directory as a static-files route.
* A stdlib consumer reads the asset files and inlines them into responses.

The create step ships **skeleton placeholder** assets; the real chrome lands
in the build-out ticket.

## Build-out phasing

The build-out should start with the **highest-overlap, lowest-risk slice**:
the shared CSS plus the column/card/move-form markup contract. Only after
that lands should it unify the JS refresh/detail behavior.

**Out of scope for this shared library:** mill-specific chrome — the agents
menu, cost dashboard, AGENT.md candidates, the repo selector, and the
proposals/runs panels all stay in robotsix-mill. Only the `#board`
columns/cards/move control, the `#drawer`/detail panel, and the refresh loop
are shared here.

## License

MIT — see the [LICENSE](LICENSE) file.

Copyright 2026 Damien Robotsix.
