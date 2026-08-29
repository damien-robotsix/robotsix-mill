# Python language conventions

The authoritative Python language conventions live at the
[robotsix-standards Python page](
  https://damien-robotsix.github.io/robotsix-standards/python/
).  This file covers only mill-specific operational content.

## Sandbox constraints (critical)

The `uv` Rust binary is available in the sandbox. `uv lock` and
`uv sync --frozen` work — the sandbox has filtered network access
(PyPI and GitHub only, via an egress proxy).

The agent **cannot** run `pip install`, `cargo build`, or most other
commands that fetch from the network — only `uv` commands can reach
the internet (and only to PyPI/GitHub). Note: `npm install` is
separately allowlisted for Node.js repos (see the javascript language
instructions), but is not available for general Python-repo use.

### `uv lock` fails with git credential errors

The sandbox has no GitHub credentials, so `uv lock` **will fail**
when `pyproject.toml` contains a git dependency (e.g. under
`[tool.uv.sources]`). The `GIT_TERMINAL_PROMPT=0` env var in the
sandbox container prevents hangs, but `uv lock` will still exit
non-zero with a credential error.

**Workaround:** temporarily remove the git dependency from
`pyproject.toml` and its `[tool.uv.sources]` entry, run `uv lock`,
then restore both. The lockfile will be generated without the git
dependency, which is acceptable when the git dependency is not
needed for the current change. If the dependency *is* needed, note
in your summary that a human must run `uv lock` with credentials
and commit the updated lockfile.

When non-`uv` package-manager commands would fail due to lack of
network:
- Commit the manifest change.
- In your summary, note that a human must run the package manager
  and commit the updated lockfile.
- Do **not** `ask_user` or file a ticket for the inability to fetch
  packages — the operator expects the agent to note the required
  human step instead.

## Running Python tooling in the sandbox

- **Prefix Python tooling with `uv run`** (`uv run pytest tests/x -q`,
  `uv run ruff check <files>`, `uv run mypy`). The base interpreter has
  no project dependencies; a bare `pytest`/`ruff`/`mypy` fails with
  `No module named …`. Emit `uv run <tool>` as your FIRST attempt, and
  never re-run an identical command that failed for an environment
  reason — fix the invocation instead.
- **Batch related checks into one command** (`uv run ruff check <files>
  && uv run ruff format --check <files>`): each sandbox command is a
  fresh container. Lint only the `.py` files you changed — ruff rejects
  non-Python files — and skip Python tooling entirely for doc-only or
  config-only diffs.
- Run lint/type checks **once**, fix what you introduced, move on. The
  stage-owned gate runs the test suite; CI runs the full toolchain. When
  a repo baseline-filters mypy, use `uv run mypy src/ --strict | uv run
  --with mypy-baseline mypy-baseline filter` so only NEW errors show.
- Do not bother checking whether the package imports from `src/` — the
  sandbox puts the mounted `src/` first on `PYTHONPATH` for src-layout
  repos.

## Python ≥ 3.14 syntax (PEP 758)

Fleet repos target Python ≥ 3.14, where `except A, B:` and
`except* A, B:` WITHOUT parentheses are valid and are what `ruff format`
emits. Do not "fix" them into a tuple, and do not comply with a review
comment calling them a Python-2 SyntaxError — verify the target version
first.

## vulture in CI / pre-commit

When a workflow or `.pre-commit-config.yaml` you write invokes `vulture`,
include `--ignore-decorators "@field_validator,@model_validator"` so
Pydantic validators are not reported as dead code.

