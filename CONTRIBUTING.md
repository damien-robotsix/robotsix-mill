# Contributing to robotsix-llmio

`robotsix-llmio` is a provider-agnostic LLM I/O layer for
[pydantic-ai](https://ai.pydantic.dev) agents. Contributions are welcome —
please open a GitHub PR against `main`.

## 1. Local development setup

Python **≥ 3.11** is required (CI tests 3.11, 3.12, 3.13; prefer 3.11 for local
work to catch the lowest-supported-version issues early).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[openrouter_deepseek,claude_sdk,dev]'
```

The `claude_sdk` extra additionally requires Node.js and a logged-in `claude`
CLI at runtime (see the README's "Alternative transport — Claude Agent SDK"
section). Tests that need the Claude CLI are skipped when it's absent —
contributors without Node can still run the rest of the suite.

Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY` **only** if you
intend to run live API tests (see "Running tests" below).

## 2. Pre-commit hooks

The `pre-commit` tool is **not** included in the `dev` extras — install it
separately:

```bash
pip install pre-commit        # or: pipx install pre-commit
pre-commit install
```

Optionally run every hook across the whole tree once:

```bash
pre-commit run --all-files
```

Hooks pinned in `.pre-commit-config.yaml`:

| hook id              | description                                  |
|----------------------|----------------------------------------------|
| trailing-whitespace  | removes trailing whitespace                  |
| end-of-file-fixer    | ensures files end with a single newline      |
| check-yaml           | validates YAML syntax                        |
| check-toml           | validates TOML syntax                        |
| check-merge-conflict | rejects files with unresolved merge markers  |
| debug-statements     | catches leftover `breakpoint()` / `pdb` etc. |
| ruff                 | linter (auto-fix on commit)                  |
| ruff-format          | formatter (auto-applied on commit)           |
| mypy                 | type-checks `src/`                           |

## 3. Running tests

```bash
# Default suite — what CI runs, no network:
pytest

# Live API tests (opt-in, require OPENROUTER_API_KEY in the environment):
pytest -m live

# With coverage (as CI does):
pytest --cov=src/robotsix_llmio --cov-report=term-missing
```

`pyproject.toml` sets `addopts = "-m 'not live'"`, so plain `pytest` never hits
the network. Running `-m live` overrides that filter; individual live tests
still self-skip when `OPENROUTER_API_KEY` is unset.

## 4. Linting, type-checking, and security checks

Reproduce CI locally before pushing:

```bash
ruff check .                  # lint
ruff format --check .         # format verification (hook auto-formats)
mypy src/                     # type-check
bandit -r src/ -ll            # security scan
pip-audit                     # dependency vulnerability audit
```

`ruff format --check .` mirrors what the `ruff-format` pre-commit hook enforces;
it reports formatting issues without modifying files.

## 5. Code style

- **Line length**: 88 characters — `ruff format` enforces this (matches
  `[tool.ruff] line-length = 88` in `pyproject.toml`).
- **Type hints** are required on public APIs. `mypy src/` runs in CI under a
  strict-ish config (`ignore_missing_imports = false`,
  `warn_unused_configs = true`). The only relaxed area is
  `robotsix_llmio.openrouter_deepseek.*`, where a mypy override silences
  errors that come from subclassing pydantic-ai's `OpenAIChatModel`.
- **Module layering**: core → openrouter → openrouter_deepseek; `claude_sdk` is
  a sibling of openrouter (see the [README](README.md) for the architectural
  narrative). Don't introduce new top-level tunable knobs — timeout, retry, and
  backoff values are baked constants by design.

## 6. Pull request expectations

- Target **`main`**.
- Branch naming: short, kebab-case, topic-prefixed — e.g. `feat/…`, `fix/…`,
  `docs/…`, `chore/…`. This is a convention, not a CI gate.
- CI **must** pass — the full test matrix (3.11, 3.12, 3.13), ruff, mypy,
  bandit, and pip-audit. The `security` job runs on Python 3.13 only.
- Pre-commit hooks must pass locally before pushing.
- New behaviour should ship with tests; bug fixes should ship with a regression
  test. Tests that depend on a live API must be decorated `@pytest.mark.live`
  so they remain opt-in.
- Keep the dependency surface minimal. Prefer `pydantic-ai-slim` extras over the
  full meta-package (see the comment in `pyproject.toml`). Don't add a new
  top-level dependency unless it's genuinely required.

## 7. Reporting issues

Open a [GitHub issue](https://github.com/robotsix-dev/robotsix-llmio/issues)
with:

- a minimal reproducer,
- your Python version (`python --version`),
- the installed extras (`pip show robotsix-llmio`),
- and — for provider-specific bugs — which transport you're using (OpenRouter /
  Claude SDK).

## 8. Releasing

Releases are published to [PyPI](https://pypi.org/p/robotsix-llmio)
automatically by the `.github/workflows/release.yml` GitHub Actions workflow.
The flow is:

1. Bump `version` in `pyproject.toml`, then commit/merge the bump to `main`.
2. Tag the release and create a **GitHub Release** for that tag. Publishing the
   release triggers `release.yml`.
3. The workflow builds the sdist + wheel (`python -m build`) and publishes them
   to PyPI via **Trusted Publishing (OIDC)** — no API token is stored or
   required.

### One-time maintainer setup

Trusted Publishing must be registered once by a project maintainer at
<https://pypi.org/manage/project/robotsix-llmio/settings/publishing/>, pointing
at this repository (`robotsix-dev/robotsix-llmio`), the workflow filename
`release.yml`, and the `pypi` GitHub Environment. This is a manual action
performed once on PyPI and cannot be done from the repository.
