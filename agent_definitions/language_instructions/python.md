# Python language conventions for the implement agent

## Manifest & lockfile workflow

- `pyproject.toml` is committed to version control.
- `uv.lock` is **generated** by `uv sync` in CI — **never** hand-edit or
  commit a lockfile directly.
- If the repo lacks CI (e.g. `.github/workflows/`) for `uv sync` + lint
  + tests, **author the CI workflow in the same ticket**. Do **not** try
  to install `uv` or generate `uv.lock` in the sandbox — the sandbox
  runs with `--network none` and cannot reach PyPI or any external
  package registry.

## Sandbox constraints (critical)

The implement sandbox runs with `--network none`. The agent **cannot**
run `uv sync`, `pip install`, `cargo build`, or any command that
fetches from the network inside the sandbox. When package-manager
commands would fail due to lack of network:

- Commit the manifest change (e.g. `pyproject.toml` updated with a new
  dependency).
- Add (or update) the CI workflow to run the install step.
- Do **not** `ask_user` or file a ticket for the inability to fetch
  packages — the operator expects the agent to compose the CI change
  instead.

## Test invocation

```bash
pytest
```

## Linter / formatter

```bash
ruff check . && ruff format .
```

## Type checker

```bash
mypy .
```