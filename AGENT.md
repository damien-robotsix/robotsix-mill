# AGENT.md — instructions for any agent (human or AI) working in this repo

This repo follows the [robotsix stack standards](https://github.com/damien-robotsix/robotsix-standards).

This is **robotsix/mill**, a personal project built solo with an AI
assistant. Optimize for a small, sharp, honest codebase — not
enterprise process. These are hard rules, learned the hard way.

## Scope & taste

- **Proportionate scope.** This is a solo hobby project. No SLAs, no
  formal policies, no compliance ceremony, no speculative
  "enterprise-grade" abstractions. Right-size everything (see the
  trimmed `SECURITY.md` for the target tone).
- **One ticket = one focused change.** If a ticket bundles N things
  (e.g. "checksum + cleanup + HEALTHCHECK + multi-stage"), it's too
  big — split it. Prefer the smallest change that solves the problem.
- Match the style, comment density, and idioms of the surrounding
  code. Don't restate what the code or git history already says.

## Repository layout

First-party packages live under the `src/<pkg>/` namespace:

- `src/robotsix_mill/` — the mill pipeline itself (agents, stages,
  core, runtime, config).
- `src/robotsix_llmio/` — the LLM I/O library (core utilities,
  config, sqlite helpers).
- Other first-party packages follow the same `src/<pkg>/` convention.

**Agents must probe `src/<pkg>/` before concluding a path is absent.**
The repo root is NOT a flat namespace — packages like `robotsix_llmio`
and `config` live under `src/`, so a bare existence check on e.g.
`robotsix_llmio/core` or `config/` at the repo root will miss. Always
try the `src/`-prefixed form when a literal file/directory probe
returns "not found". The `read_file` and `list_dir` tools already
apply this fallback transparently, but agents performing their own
existence reasoning (e.g. scope analysis, draft-routing checks) must
apply the same rule — never assume only `src/robotsix_mill/` exists
under `src/`.

## The test gate is sacred and must stay hermetic

- The implement stage gates on the **full pytest suite running inside
  the container**. It must be green there, not just on a dev machine.
- **Tests never touch the network and never consume tokens.**
  `tests/conftest.py` strips every credential/endpoint env var and
  hard-blocks real `httpx` transports. Keep it that way. Always mock
  the model / HTTP seam (`build_agent`, the `run_*_agent` seam, or
  `httpx`). A test that needs a real key or a real request is wrong.
- Run the suite before you commit. Add/adjust tests with the change.

## Testing conventions

- **Test browser scripts** (e.g. `src/robotsix_mill/runtime/static/board.js`)
  with a Node `vm`-based harness in `tests/runtime/` that uses ONLY Node
  built-ins (`node:fs`, `node:vm`, `node:assert`, etc.) and is driven by a
  pytest wrapper that shells out to `node` and `pytest.skip`s when node is
  absent. Never introduce an npm-based JS test runner
  (vitest/jest/jsdom/playwright) or a Node setup step in CI — the test
  sandbox is network-isolated and CI runs only pytest.

## Board UI

- The kanban CSS/JS live in `src/robotsix_mill/runtime/static/`
  (`board.css`, `board.js`), served via `StaticFiles`.
- **Never inline JS/CSS back into the `board_html.py` Python string.**
  A `\n` in a Python-embedded JS string becomes a real newline and
  silently breaks the entire board. Put JS in `board.js`.

## Git / CI

- `git fetch && git rebase origin/main` **before** committing. The
  mill merges autonomously; assume `main` moved.
- **Never weaken a quality/security gate to make CI go green.** Don't
  lower a Trivy severity, flip an `exit-code`, relax a lint threshold,
  or broaden an ignore. Fix the real cause, or add a *narrow,
  justified, commented* ignore entry.
- **Every new CI check must gate or be removed.** A new check either
  fails the build on findings, or is explicitly documented as an
  accepted advisory policy (a comment in the config file/workflow
  **and** a note in `CONTRIBUTING.md`'s "CI overview"). A check that
  does neither must be removed — consult
  [docs/ci-policy.md](docs/ci-policy.md) before adding any CI step.
- Don't reintroduce a regression a test or this file already guards.

## Agent behavior

- `report_issue` is for a real blocking/degrading problem you hit
  while working — never a "nothing to report / clean run" no-op.
- Respect the sandbox and path-confinement; never bypass isolation or
  exfiltrate secrets. The management API stays unauthenticated +
  localhost-only by design.
- If something is genuinely underspecified or a tool is missing, say
  so (or `report_issue`) — don't guess and gold-plate.

## Agent definition conventions

Every agent definition under `agent_definitions/` **must** set a
`category` matching its runtime role. The five valid categories,
validated by `test_category_is_valid` in
`tests/agents/test_yaml_loader.py` (the `_VALID_CATEGORIES` frozenset),
are:

- **`pipeline`** — stage agents called by the pipeline state machine
  (e.g. `refine.yaml`, `implement.yaml`, `review.yaml`, `triage.yaml`,
  `document.yaml`, `retrospect.yaml`, `dedup.yaml`,
  `epic_breakdown.yaml`, `obsolescence.yaml`, `auto-approve.yaml`,
  `doc_classifier.yaml`, `maintenance.yaml`, `scope_triage.yaml`,
  `spec-review.yaml`, `tester.yaml`, `pipeline/meta_triage.yaml`).

- **`periodic`** — background scheduled agents, almost always under
  `agent_definitions/periodic/` (e.g. `periodic/audit.yaml`,
  `periodic/health.yaml`, `periodic/survey.yaml`,
  `periodic/meta.yaml`, `periodic/test_gap.yaml`,
  `periodic/agent_check.yaml`, `periodic/bc_check.yaml`,
  `periodic/completeness_check.yaml`,
  `periodic/copy_paste.yaml`, `periodic/cost_analyst.yaml`,
  `periodic/diagnostic.yaml`, `periodic/forge_parity.yaml`,
  `periodic/module_curator.yaml`, `periodic/run_health.yaml`, and
  `epic_status.yaml` at the root).

- **`sandboxed`** — agents that execute in ephemeral sandboxes
  (e.g. `ci_fix.yaml`, `rebase.yaml`, `review_revision.yaml`).

- **`interactive`** — prompt-to-ticket or Q&A agents triggered by user
  interaction (e.g. `ask_to_ticket.yaml`, `answer.yaml`).

- **`sub_agent`** — utility agents called by other agents as a tool
  (e.g. `explore`, `web_research`, `trace_inspector`). These live in
  `agent_definitions/` but their definitions are tool-wired, not
  pipeline-dispatched.

Validation lives in `tests/agents/test_yaml_loader.py` — there is no
Pydantic validator on the production `AgentDefinition` model. See
`docs/agent-yaml-schema.md` for the full field reference.

## Meta-agent

The **meta-agent** is a cross-repo survey agent that runs **daily**
(86400 s interval) as a single global pass — not per-repo. It clones
all registered repositories, compares their codebases, and files:

- **Extraction proposals** (shared abstractions warranting a standalone
  library) on the **meta board** (`board_id: "meta"`).
- **Alignment proposals** (practice divergence — one repo has a
  pattern another repo should adopt) on the target repo's own board.

Registration steps:
1. Add the `meta` stanza to `config/repos.yaml` (see `docs/meta-board.md`).
2. Set `MILL_META_PERIODIC=true` (or `meta_periodic: true` in YAML config).
3. Restart the worker — the daily pass begins on the next tick.

The meta board is synthetic (no backing forge repository); tickets
live purely in the ticket system. The agent definition lives at
`agent_definitions/periodic/meta.yaml`.

## Reference docs: `agent_references/`

Stack-specific gotchas live under `agent_references/` — one Markdown
file per topic (e.g. `agent_references/sqlalchemy-sqlite.md`). They
are **not** auto-injected into any agent's prompt; an agent that is
about to touch a stack covered there is expected to `read_file` the
matching entry first. Spec writers (refine) should NOT pre-prescribe
the workaround — let the implement agent consult the reference when
it has the actual code in front of it.

When you discover a new stack-level trap that another agent will hit:
add a new `agent_references/<topic>.md` describing it in the same
shape as the existing entry (limitation → consequence → canonical
workaround). Keep entries narrow and verifiable in the repo.

## Module taxonomy

The repo has a formal module taxonomy so that navigation-heavy agents
can understand the codebase structure without crawling it from scratch.

**`docs/modules.yaml`** is the single source of truth for what modules
exist and where their files live. It's a YAML list under a `modules:`
key, where each entry has:

- `id` — stable kebab-case identifier (e.g. `config`, `agent-infra`).
  Must match `^[a-z][a-z0-9]*(-[a-z0-9]+)*$`.
- `description` — one-paragraph summary of the module's responsibility.
- `paths` — repo-relative glob patterns covering every file belonging
  to that module.
- `dependencies` — array of other module `id` values this module
  structurally depends on (not import-level).

Every tracked file in `src/`, `tests/`, `docs/`, etc. should be claimed
by exactly one module. There are currently 19 modules; the canonical
list is `docs/modules.yaml` itself. The file is validated on every push
by `docs/modules.schema.yaml` (JSON Schema, draft 2020-12) via a
pre-commit hook and CI step, both running
`robotsix-modules validate` (from the `robotsix-modules` dev dependency).

### `module_curator` periodic agent

A read-only agent runs daily (configurable via
`module_curator_interval_seconds` in `config/config.example.yaml`,
default 86400 s). It **never** moves files, deletes files, or edits
`docs/modules.yaml`. Its tools are `explore`, `read_file`, `list_dir`,
and `run_command`.

It detects three classes of drift:

1. **Unclassified files** — files in covered directories not matched by
   any module's `paths` globs. Files a draft ticket titled
   "Classify `<file>`: assign to existing module or propose a new one."
2. **Stale paths** — module globs that resolve to zero existing files.
   Files a draft ticket titled "Cleanup module `<id>`: path `<glob>`
   references no files."
3. **New module proposals** — spots a `## New module` section in a
   recently merged PR description and files a draft ticket titled
   "Ratify new module: `<id>`."

The curator de-duplicates against existing open tickets before filing.
Its memory ledger lives at `<data_dir>/module_curator_memory.md` and
persists state across passes.

### `modules: true` opt-in for agents

Agent definition YAMLs (in `agent_definitions/` and
`agent_definitions/periodic/`) have an optional `modules` boolean
field, defaulting to `false`. When an agent definition sets
`modules: true`, `compose_prompt()` in `src/robotsix_mill/agents/base.py`
loads `docs/modules.yaml`, renders a compact **Module Map** block via
`_render_module_map()`, and appends it to the agent's system prompt.
The Module Map lists each module's `id`, `description`, `paths`, and
`dependencies`. When there are more than 20 modules, only top-level
modules (those with no `dependencies`) are shown, with a pointer to
`docs/modules.yaml` for the rest.

**Currently no agents have opted in** — the `modules` field is absent
from all agent YAML files. Navigation-heavy agents (e.g. `refine`,
`implement`, `explore`) are the intended consumers; opt-in happens in a
separate ticket.

### Adding a new module

1. Add a new entry to `docs/modules.yaml` under `modules:` with `id`,
   `description`, at least one `paths` glob, and `dependencies` (can
   be `[]`).
2. The pre-commit hook and CI will catch schema violations — no
   manual validation step is needed. The `module_curator` will flag
   any files that still fall outside the taxonomy.

### Deprecating a module

Remove the module entry from `docs/modules.yaml` and reassign its
`paths` to the module(s) that absorbed the responsibility. The pre-commit hook and CI will validate the result automatically. The curator may
file "stale paths" and "Classify" tickets as reminders — these aren't
errors, just prompts to clean up.

### Adding a tracked file

When adding a new source or test file that belongs to a tracked module,
add its path entry to `docs/modules.yaml` in the same commit that
introduces the file — symmetric to `### Deleting a tracked file` below.
Classifying the file in-commit prevents the registry from omitting it
and saves the `module_curator` from filing a "Classify" drift ticket
after the fact. The CI path-lint only checks that existing entries'
paths resolve to real files; it does not require new files to be added,
so this stays a convention enforced at authoring time.

### Placing a module's tests

Place a new module's tests under `tests/<module>/` matching that
module's name in `docs/modules.yaml`; never default to `tests/runtime/`
or the `tests/` root. Add the new test file's path to the module's
`paths` entry in `docs/modules.yaml` in the same commit.

### Deleting a tracked file

When deleting a source or test file tracked in `docs/modules.yaml`,
remove its path entry from `docs/modules.yaml` in the same commit as
the deletion. Unlike `### Deprecating a module` (which removes an entire
module entry), this covers deleting *individual files* while the module
persists — drop only the affected `paths` glob/entry, not the whole
module. Doing this in the deleting commit prevents the registry from
referencing nonexistent files and saves the `module_curator` from
filing a "stale paths" cleanup ticket after the fact.

## Forge adapter conventions

- **Public-method / private-HTTP-seam split.** Every new abstract
  method added to `Forge` (`src/robotsix_mill/forge/base.py`) must
  follow the two-layer pattern established in `GitHubForge`
  (`src/robotsix_mill/forge/github.py`). The **public method**
  performs validation and feature-flag checks, raises
  `NotConfiguredError` when the capability is gated, then delegates
  to the **private `_method_name()`**. The private method is the
  monkeypatch-able HTTP seam — use `_build_headers(github_token(...))`
  and `with httpx.Client(timeout=30) as c:` to talk to the API, exactly
  as the existing private helpers do (e.g. `_create_repo`,
  `_create_pr`, `_get_pr`).
