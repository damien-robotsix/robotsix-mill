# Contributing

## Development quickstart

Python **3.14** is required (see [`.python-version`](.python-version)).

```bash
cp config/config.example.json config/config.json   # fill in secrets.openrouter_api_key at minimum
make install                  # editable install into .venv with dev+tracing extras
make dev                      # hot-reload server on http://127.0.0.1:8077
make test                     # pytest with coverage (fail-under 70%)
```

Other `make` targets:

| Target   | Description |
|----------|-------------|
| `serve`  | Production-style server (YAML config + `config/secrets.yaml`, data in `.mill-data`) |
| `docker` | `docker compose up -d --build` |
| `clean`  | Remove `.venv`, `.mill-data`, `.pytest_cache`, and all `__pycache__` dirs |

Install pre-commit hooks (Ruff, mypy, Bandit):

```bash
.venv/bin/pre-commit install
```

Also opt into `.git-blame-ignore-revs` locally so `git blame` skips
bulk-format/restructure commits:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

### Dependencies

`uv.lock` is **committed** and is the source of truth for installs:
`make install` and CI run `uv sync --frozen` against it (no `uv lock`
regeneration), so `main` builds off pinned commits. The four
`robotsix-*` shared libraries are git deps tracking `@main`; new commits
advance into mill only through the automated `uv.lock` bump PR
([`deps-bump.yml`](.github/workflows/deps-bump.yml)). See
[docs/dependencies.md](docs/dependencies.md) for the full pin + bump
mechanism, trade-offs, and the CI-monitor heuristic.

## Project structure

## Layout

| Path | Role |
|---|---|
| `config.py` | settings (YAML pipeline + env vars) + secrets model |
| `core/states.py` | state machine (single source of truth) |
| `core/models.py` | SQLModel tables + API schemas |
| `core/db.py` · `core/service.py` | DB lifecycle + management-plane operations |
| `core/workspace.py` | per-ticket file workspace (file-canonical body) |
| `runtime/worker.py` | event-driven queue + stage chaining (+ audit/trace-health/cost-sync poll) |
| `runtime/api.py` | FastAPI app (API + worker lifespan + audit/trace-health route) |
| `runtime/tracing.py` | Langfuse tracing + OpenRouter cost ✅ |
| `sandbox.py` | isolated command execution (always containerized) |
| `stages/` refine·implement·deliver·merge·retrospect | ✅ all real |
| `dedup.py` | shared ticket-dedup primitives (refine + trace-review + epic-decomposition) |
| `audit_runner.py` | audit pass orchestration |
| `trace_health_runner.py` | trace-health check orchestration |
| `agents/auditing.py` | audit agent (meta-audit for gaps) |
| `forge/github.py` · `forge/auth.py` | GitHub PR/status + PAT/App-bot auth ✅ |
| `langfuse/client.py` | read-side session summary + trace listing + session total cost (retrospect + trace-health + cost sync) |
| `agents/coding.py` · `fs_tools.py` · `retrospecting.py` | agents + sandboxed tools |
| `vcs/git_ops.py` | clone / branch / commit / push helpers |

Here's how the key directories relate:

- **`agents/`** — LLM-driven logic. Each agent is built by
  `agents/base.py:build_agent()` with a system prompt, tools, and a
  per-role model. Agent modules are pure logic (no I/O orchestration).
- **`stages/`** — Pipeline steps (refine → implement → deliver →
  merge → retrospect). Stages call agents and handle workspace I/O.
- **`*_runner.py`** — Periodic pass orchestration (audit, health,
  agent-check, trace-health). Each runner reads a memory ledger, invokes
  an agent, writes back, and emits draft tickets.
- **`core/`** — DB models, state machine, ticket service, workspace.
- **`runtime/`** — FastAPI app, worker pool, poll loops, tracing.

The full architecture is covered in [README.md](README.md). For Docker
setup and GitHub App delivery identity, see
[docs/docker-architecture.md](docs/docker-architecture.md) and
[docs/github-app.md](docs/github-app.md).

### Epic-decomposition pre-filing dedup

When an epic is decomposed into children, an advisory pre-filing dedup
check (in `dedup.py`) flags a would-be child that duplicates either a
recently shipped/in-flight ticket or an earlier sibling in the same
batch. Overlaps are logged and annotated onto the child's body, but the
child is **never dropped** — the check is advisory only. See
[docs/epic-dedup.md](docs/epic-dedup.md) for the authoritative reference.

## How the agent pattern works

Every agent is defined by a YAML file in `agent_definitions/` and a
Python module in `src/robotsix_mill/agents/`. The YAML file declares
the prompt, tools, model binding, output type, and metadata; the
Python module supplies the output model class and the entry function.

### YAML-first workflow (preferred for new agents)

1. Create `agent_definitions/<name>.yaml` with the agent's fields
   (see [`docs/agent-yaml-schema.md`](docs/agent-yaml-schema.md) for
   the full reference, and `agent_definitions/refine.yaml` as the
   canonical example).

2. Create `src/robotsix_mill/agents/<module>.py` with:
   - The Pydantic output model (if `output_type` is set in the YAML)
   - An entry function that calls `build_agent_from_definition()`
     from `base.py`, passing optional `**overrides` for runtime
     decisions (tools, conditional prompts).

3. The entry function pattern:

```python
from robotsix_mill.agents.base import build_agent_from_definition
from pathlib import Path

_YAML = Path("agent_definitions/refine.yaml")

def run_refine_agent(*, settings, title, draft, repo_dir=None, ...):
    definition = load_agent_definition(_YAML)
    tools = [...]  # optional: runtime-conditional tools
    agent = build_agent_from_definition(
        _YAML, settings,
        tools=tools,
        # optional overrides: model_name=..., system_prompt=...
    )
    ...
```

### Direct factory (legacy, for sub-agents and special cases)

Some agents (sub-agents, rebase, ci-fix) still construct via
`build_agent()` directly because they have conditional prompts or
runtime-specific tool sets that don't map cleanly to YAML.

```python
from robotsix_mill.agents.base import build_agent

agent = build_agent(
    settings,
    system_prompt="You are a ...",
    output_type=str,
    tools=[...],           # optional role-specific tools
    web=False,             # set True to add the web_research sub-agent tool
    model_name=settings.audit_model,  # per-agent model override
)
```

`build_agent()` lazily imports `pydantic_ai`, wires a
`CostInstrumentedOpenRouterModel` via `OpenRouterProvider`, injects
skills into the system prompt, and **always** attaches the
`report_issue` tool (so every agent can file a draft ticket when it
hits a problem). When `web=True`, it also attaches a `web_research`
tool backed by a cheap sub-agent — the main model never uses
OpenRouter's `:online` surcharge.

Each agent role (coordinator, refiner, auditor, explore, etc.) gets its
own model name from `Settings` — only the prompt, tools, and model
differ. See [`agents/`](src/robotsix_mill/agents/) for the full set.

## Adding a new periodic agent

Three artifacts are needed. Use **audit** and **health** as reference
implementations (they are the simplest end-to-end examples):

1. **YAML definition** — `agent_definitions/<name>.yaml`
   - Declare `name`, `description`, `category`, `model`, `system_prompt`,
     `tools`, `web`, `report_issue`, `output_type`, `module`, and `skills`.
   - See [`agent_definitions/refine.yaml`](agent_definitions/refine.yaml)
     for the canonical example and
     [`docs/agent-yaml-schema.md`](docs/agent-yaml-schema.md) for the
     field reference.

2. **Agent module** — `agents/<name>.py` (or `<module>.py` if different)
   - Define the Pydantic output model named by `output_type`.
   - Export a `run_<name>_agent()` function that loads the YAML via
     `load_agent_definition()`, builds the agent via
     `build_agent_from_definition()`, and returns the structured result.

3. **Runner module** — `runners/<name>_runner.py` in the runners subpackage
   - Read the memory ledger → run the agent → write back → emit draft
     tickets via `TicketService`.
   - See [`audit_runner.py`](src/robotsix_mill/runners/audit_runner.py) and
     [`health_runner.py`](src/robotsix_mill/runners/health_runner.py).

4. **Wiring** — three touchpoints:
   - **CLI**: add a subcommand in [`cli.py`](src/robotsix_mill/cli.py)
     (e.g. `robotsix-mill audit`).
   - **API**: add a route in [`runtime/api.py`](src/robotsix_mill/runtime/api.py)
     (e.g. `POST /audit`).
   - **Worker**: add an opt-in poll loop in
     [`runtime/worker.py`](src/robotsix_mill/runtime/worker.py)
     (gated by a `MILL_<NAME>_PERIODIC` setting).

## Testing guide

Tests live in `tests/` and mirror the source tree
(`test_implement.py`, `test_audit.py`, etc.).

**Configuration** ([`pyproject.toml`](pyproject.toml)):
- `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.
- Coverage fail-under 70% (via `pytest-cov`).
- Run with `make test`.

**Key fixtures** ([`tests/conftest.py`](tests/conftest.py)):
- `fake_sandbox` — monkeypatches `sandbox.run` and `sandbox.fetch` with
  a tiny shell interpreter (supports `echo`, `false`, `true`). No
  Docker needed for tests.
- `_no_dotenv` (autouse) — clears all ambient credential/endpoint env vars
  so tests stay hermetic (``Settings`` reads only ``os.environ`` and
  ``config/*.yaml``; ``.env``/``secrets.env`` are no longer loaded).
- `settings` — inits an isolated SQLite DB in `tmp_path` with
  `MILL_REQUIRE_APPROVAL=false`.
- `service` — a `TicketService` bound to the test settings.

**Fake-agent closures** ([`tests/test_implement.py`](tests/test_implement.py)):
- `_fake_agent(write)` returns a callable matching
  `run_implement_agent`'s keyword-only signature.
- `write=None` → agent returns `("did the thing", [])` without writing
  files.
- `write={"file.txt": "content"}` → agent writes the given files into
  the repo dir.
- For budget/error scenarios, test authors inline a custom `_run` that
  raises the relevant exception.
- Patch with `monkeypatch.setattr(coding, "run_implement_agent", ...)`.

## Code style

All enforced by [`.pre-commit-config.yaml`](.pre-commit-config.yaml):

| Tool   | Version | Configuration |
|--------|---------|---------------|
| Ruff (lint + format) | v0.11.0 | `ruff --fix` + `ruff format` |
| mypy | v1.15.0 | `--strict --ignore-missing-imports`; stubs for `pydantic-ai-slim`, `sqlmodel`, `fastapi` (CI: baseline ratchet via `mypy-baseline` — see CI overview) |
| Bandit | 1.8.3 | Config from `pyproject.toml`; targets `src/`, skips `B101` (assert) |
| hadolint | v2.14.0 | `--failure-threshold warning` (advisory/non-gating); config from `.hadolint.yaml` |
| pre-commit-hooks | v5.0.0 | trailing-whitespace, end-of-file-fixer, check-yaml, check-json, check-toml, check-merge-conflict, detect-private-key, check-added-large-files (500 KB max) |
| detect-secrets | v1.5.0 | `--baseline .secrets.baseline`; blocks new secrets not in the allowlist |
| zizmor | v1.25.2 | `--offline`; audits GitHub Actions workflows for supply-chain risks |

Install with `.venv/bin/pre-commit install`. Run manually:
`pre-commit run --all-files`.

Also install the commit-msg hook so `commitizen` validates every commit
message against [Conventional Commits](https://www.conventionalcommits.org/):

```bash
.venv/bin/pre-commit install --hook-type commit-msg
```

## Commit messages

This repo enforces [Conventional Commits](https://www.conventionalcommits.org/)
via the `commitizen` commit-msg hook (see [`.pre-commit-config.yaml`](.pre-commit-config.yaml)).
Every commit message must follow the format:

```
<type>(<scope>): <description>

[optional body]

[optional footer(s)]
```

Where `<type>` is one of: `feat`, `fix`, `docs`, `style`, `refactor`,
`perf`, `test`, `build`, `ci`, `chore`, `revert`. Breaking changes are
signaled by appending `!` after the type/scope (e.g. `feat!: drop
Python 3.13 support`) or by including `BREAKING CHANGE:` in the footer.

Commit messages keep the history readable and reviewable for contributors.

## PR checklist

- [ ] Tests pass: `make test`
- [ ] Pre-commit clean: `pre-commit run --all-files`
- [ ] New modules have tests in `tests/`
- [ ] New public functions have docstrings
- [ ] No new Bandit findings (existing `B101` skip is expected)

## CI overview

The governing policy for every CI check is
[docs/ci-policy.md](docs/ci-policy.md) — the **gate-or-remove**
principle (every check must gate, be documented as accepted advisory,
or be removed) plus a reviewer checklist for new checks. The per-check
notes below remain the authoritative rationale for each individual
check.

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| [`docker-publish.yml`](.github/workflows/docker-publish.yml) | Push to `main` | hadolint lint → build Docker image → Trivy CRITICAL scan → push to Docker Hub with SBOM + SLSA attestation |
| hadolint gate (in `docker-publish.yml`) | (within `docker-publish.yml` push) | `hadolint/hadolint-action@v3.3.0` with `failure-threshold: warning` |
| [`security-audit.yml`](.github/workflows/security-audit.yml) | Push/PR to `main`, weekly cron | `pip-audit` (CVEs) + `pip-licenses` (license allowlist gate) on installed dependencies |
| [`ci.yml`](.github/workflows/ci.yml) | Push/PR to `main` | `uv sync --frozen` (committed-lock gate — fails on a stale `uv.lock`) → deptry → **dependency audit** (CVE scan via `uv audit --frozen --preview` / `pip-audit` fallback — **hard gate**) → module taxonomy → Ruff → mypy `--strict` (advisory) → Bandit MEDIUM+ (advisory; see `[tool.bandit]`) → pytest (70% cov) |
| [`dependency-review.yml`](.github/workflows/dependency-review.yml) | PR to any branch | `actions/dependency-review-action@v5.0.0` with `fail-on-severity: moderate` — analyzes the *delta* of dependency manifests (e.g. `pyproject.toml`, `uv.lock`) between the PR and its base branch, blocking on new or upgraded dependencies that introduce vulnerabilities rated moderate or higher |
| [`deps-bump.yml`](.github/workflows/deps-bump.yml) | Weekly cron + manual | `uv lock --upgrade` → opens a PR refreshing `uv.lock` (shared-lib `@main` bumps), gated by `ci.yml` on the PR — see [docs/dependencies.md](docs/dependencies.md) |
| [`release.yml`](.github/workflows/release.yml) | Push to `main` | hadolint lint on all three Dockerfiles → build and publish Docker images to GHCR (`robotsix-mill`, `robotsix-mill-sandbox`, `robotsix-mill-sandbox-proxy`) via the shared reusable `docker-release.yml` workflow → smoke-test each published image (entrypoint reachable, agent definitions bundled, version readable). |

**Note on the hadolint gate in `docker-publish.yml`:** hadolint runs with
`failure-threshold: warning` on every push to `main`. Warnings are
visible in CI annotations and logs but do **not** block the pipeline.
This is a deliberate, documented advisory policy: the warning-level
rules that fire are worth surfacing as review feedback, but the
current Dockerfiles are functional and gating on warnings would add
friction without proportional benefit — the same pattern this repo
uses for the mypy advisory gate and the Bandit severity floor. The
threshold is intentionally not `error`; once the Dockerfiles mature
past the current known warnings the gate may be promoted to a hard
gate (analogous to the mypy backlog cleanup plan). See
[`.hadolint.yaml`](.hadolint.yaml) for per-rule rationale.

**Note on the mypy step in `ci.yml`:** mypy `--strict` runs on every
PR/push through a **baseline ratchet** (`mypy-baseline filter`).
The committed `mypy-baseline.txt` captures all known pre-existing
strict-mode errors (~1,100 across ~140 files), which are tolerated.
Any *new* error not in the baseline blocks CI — preventing regressions
while the backlog is burned down incrementally. A **separate,
non-blocking** `mypy-baseline suggest` step (`--exit-zero`) runs
alongside the gating ratchet and surfaces one backlog item per run as a
CI annotation to encourage incremental burndown of the baseline. Like
the hadolint gate and the Bandit severity floor, this is a deliberate,
documented advisory policy: it surfaces feedback without blocking the
pipeline, and is distinct from the gating `mypy-baseline filter` ratchet
above.

**To shrink the baseline** after fixing type errors: run
`uv run mypy src/ --strict | uv run mypy-baseline sync` and commit
the updated (smaller) `mypy-baseline.txt`. When the baseline file
becomes empty the ratchet step can be replaced with a direct
`uv run mypy src/ --strict` hard gate.

**Note on the dependency review gate in `dependency-review.yml`:**
`actions/dependency-review-action` runs on every PR with
`fail-on-severity: moderate`. Unlike `pip-audit` (which scans the
*entire* installed dependency tree), this check analyzes only the
*delta*: it compares the PR's dependency manifests (`pyproject.toml`,
`uv.lock`) against the base branch's manifests. PRs that add a new
dependency with a known vulnerability, or upgrade an existing one to a
vulnerable version, are blocked — without requiring the full
environment to be installed. `moderate` avoids noise from `low`-severity
alerts while catching genuinely actionable vulnerabilities. This is a
hard **gate**, not advisory. Promotion path: none (already a gate).

**Note on the license gate in `security-audit.yml`:** the `license-audit`
job runs `pip-licenses` over the installed `.[tracing]` dependency tree
with an **allowlist** (`--allow-only`) of permissive licenses (MIT,
Apache-2.0, BSD 2/3-clause, ISC, PSF, MPL-2.0, Unlicense). Because it is
an allowlist, it fails on **anything** not explicitly permitted —
copyleft (GPL/AGPL/LGPL) *and* unlicensed/`UNKNOWN` dependencies — so a
restrictively-licensed transitive dep cannot slip into this MIT project
undetected. This is a hard **gate**, not advisory. The allowlist strings
are the exact license names `pip-licenses --from=mixed` emits from
package metadata (not bare SPDX ids). The per-package **escape hatch** is
`--ignore-packages`, used for the first-party MIT robotsix git
dependencies that can report `UNKNOWN` license metadata, plus the rare
third-party package that is verifiably permissive but whose metadata
format defeats the allowlist (e.g. `tiktoken`, which ships the full MIT
license text as its `License` field with no Trove classifier, so
`--from=mixed` emits the whole blob instead of a short token); each
suppression carries an inline justification in the workflow, the same
pattern as pip-audit's `--ignore-vuln`. Policy lives entirely in the workflow's CLI
flags and comments — there is intentionally no separate
`.licenserc`/`.scancode` config file.

### Trivy vulnerability scanning

The `docker-publish.yml` workflow runs two separate Trivy steps against
the built Docker image:

**Gate (blocking)** — Only **CRITICAL** CVEs with an available fix
(`ignore-unfixed: true`) fail the pipeline. HIGH, MEDIUM, and LOW
findings do **not** gate because they are high-volume and low-signal in
container base images; gating on them would create a noisy, red-CI
state that contributors cannot actionably resolve. Restricting the gate
to CRITICAL + fixable targets the narrow, must-fix subset where a
remediation is actually available.

**Escape hatch** — [`.trivyignore`](.trivyignore) at the repo root
accepts per-CVE suppressions with `# expires: YYYY-MM-DD` annotations.
This is the mechanism for known false positives (e.g. a CRITICAL CVE in
a transitive library that is never invoked in this deployment model).
When the expiry date passes the suppression must be re-evaluated or
removed — the gate resumes failing if the CVE is still detected.

**Observability layer (non-blocking)** — A separate SARIF run emits
findings for **all** severities (no exit-code) and uploads them to the
GitHub Security tab via `codeql-action/upload-sarif`. This gives
contributors full visibility into the vulnerability surface without
blocking merges on non-actionable findings.

**Why two separate steps?** The gate and SARIF runs are intentionally
separate because the `trivy-action` entrypoint unsets `TRIVY_SEVERITY`
when `format=sarif` (unless `limit-severities-for-sarif=true`).
Combining them into one step would silently scan **all** severities and
fail on HIGH/MEDIUM, defeating the CRITICAL-only gate.

Dependabot (weekly) keeps pip and Docker dependencies current.
