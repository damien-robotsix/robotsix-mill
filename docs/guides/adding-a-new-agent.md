# Adding a New Agent

This guide walks through the end-to-end process of adding a new
periodic agent to robotsix/mill, using the `health` agent as a worked
example.  By the end you will understand every touch-point — from the
YAML definition through to the test suite.

## Prerequisites

- **Naming convention.**  Use `snake_case` for the agent name, its
  YAML file, its Python module, and all derivative identifiers
  (tasks, config keys).  The convention is already followed by every
  existing agent (`bc_check`, `health`, `agent_check`, …).

- **SourceKind registration.**  Every periodic agent needs a member
  in the `SourceKind` enum (`src/robotsix_mill/core/models.py`).  The
  member value is the ticket-source label stamped on draft tickets
  the agent files.  For `health` it is `HEALTH = "health"`; add yours
  in the same enum block (sorted roughly alphabetically).

- **Module taxonomy.**  If your agent introduces a new Python module,
  add it to `docs/modules.yaml` in the same commit (see
  [AGENT.md](../../AGENT.md#adding-a-tracked-file)).

---

## Step 1 — YAML definition

Create `agent_definitions/periodic/<name>.yaml`.  This is the
declarative contract that the loader, the periodic runner, and the
worker all read.  A minimal example (see
`agent_definitions/periodic/health.yaml`):

```yaml
name: health
description: >-
  Periodic codebase-health inspection across six dimensions
  (module size, function length, doc coverage, test gaps,
  complexity hotspots, dead code).

category: periodic

interval: 1d
enabled: true

model: ${MILL_HEALTH_MODEL}

tools:
  - explore
  - read_file
  - list_dir

web: false
report_issue: false
close_thread: false
list_threads: false
read_ticket: true

output_type: HealthResult
retries: 4

module: health

skills: [board-read]

system_prompt: |
  You are a codebase-health inspection agent …
```

### Schema reference

The complete field reference lives at [Agent YAML Schema](../agent-yaml-schema.md).
Key points for a periodic agent:

| Field | Notes |
|---|---|
| `name` | Unique; matches the YAML filename stem. |
| `category` | Must be `"periodic"` for periodic agents. |
| `interval` | Human-readable (`1d`, `2h30m`, `1w`). |
| `enabled` | `true` or `false`; can be overridden per-repo. |
| `model` | Literal (`openai/gpt-4o-mini`) or env-var (`${MILL_HEALTH_MODEL}`). |
| `output_type` | The Pydantic model class name from your Python module. |
| `module` | The Python module under `src/robotsix_mill/agents/`. |
| `tools` | Usually `explore`, `read_file`, `list_dir` for read-only agents. |
| `web` | `false` for agents that only inspect the local repo. |
| `report_issue` | `false` when the agent emits structured draft tickets. |
| `read_ticket` | `true` so the agent can cross-reference past proposals. |

---

## Step 2 — Python module

Create `src/robotsix_mill/agents/<name>.py`.  The module must:

1. **Load the system prompt** from the YAML via
   `load_periodic_system_prompt`.
2. **Alias the result type** (usually `PeriodicAgentResult`).
3. **Define the entry function** that delegates to
   `run_periodic_agent`.

Here is `health.py` in full:

```python
"""The health agent: codebase-health inspection …"""

from __future__ import annotations

from ..config import Settings
from .periodic_base import PeriodicAgentResult, load_periodic_system_prompt

SYSTEM_PROMPT: str = load_periodic_system_prompt("health")

MAX_GAPS = 8

HealthResult = PeriodicAgentResult


def run_health_agent(
    *,
    settings: Settings,
    memory: str = "",
    recent_proposals: str = "",
    verified_proposals: str = "",
    repo_dir=None,
    definition_override=None,
) -> HealthResult:
    from .periodic_base import run_periodic_agent

    return run_periodic_agent(
        settings=settings,
        definition_name="health",
        definition_override=definition_override,
        model_setting=settings.health_model,
        max_gaps=MAX_GAPS,
        repo_dir=repo_dir,
        memory=memory,
        recent_proposals=recent_proposals,
        verified_proposals=verified_proposals,
        prompt_tail="Perform the health inspection and return your result.",
        include_forge_url=True,
    )
```

### Signature contract

The entry function must accept these keyword-only parameters:

| Parameter | Type | Purpose |
|---|---|---|
| `settings` | `Settings` | Application configuration (models, paths, tokens). |
| `memory` | `str` | The agent's persistent memory ledger (Markdown). |
| `recent_proposals` | `str` | Summaries of the agent's existing board tickets. |
| `verified_proposals` | `str` | Summaries of recently verified proposals. |
| `repo_dir` | `str \| None` | Path to the local repository clone. |
| `definition_override` | `dict \| None` | Per-repo YAML overrides. |

The `model_setting` parameter of `run_periodic_agent` is the
`Settings` attribute that holds the model name (e.g.
`settings.health_model`).  It must match the config key defined in
Step 4.

---

## Step 3 — Register the pass

Open `src/robotsix_mill/runners/periodic_runner.py` and add an entry
to `PERIODIC_PASS_CONFIGS` (a `dict[str, PeriodicPassConfig]`).

For `health`:

```python
HealthPassResult = PeriodicPassResult  # at module top-level

PERIODIC_PASS_CONFIGS: dict[str, PeriodicPassConfig] = {
    …
    "health": PeriodicPassConfig(
        label="health",
        source_kind=SourceKind.HEALTH,
        agent_module_attr="health",
        agent_fn_name="run_health_agent",
        memory_filename="health_memory.md",
        workspace_subdir="health_workspace",
        result_dataclass=HealthPassResult,
        clone_token_fn=None,
    ),
    …
}
```

Also define a `HealthPassResult` alias at module top-level (like
`BcCheckPassResult` at line 38).  The simplest form is:

```python
HealthPassResult = PeriodicPassResult
```

or create a dedicated `@dataclass` if you need extra fields.

### Create a thin runner stub

Create `src/robotsix_mill/runners/<name>_runner.py`.  It is a
backward-compatibility shim that lets the HTTP route layer (Step 6)
call into the generic `run_periodic_pass`.  Example (`health_runner.py`):

```python
"""Health runner — backward-compat stub. See periodic_runner."""

from __future__ import annotations

from ..config import RepoConfig, Settings
from .periodic_runner import (
    HealthPassResult,
    PERIODIC_PASS_CONFIGS,
    run_periodic_pass,
)


def run_health_pass(
    session_id: str, repo_config: RepoConfig | None = None
) -> HealthPassResult:
    settings = Settings()
    return run_periodic_pass(
        session_id,
        repo_config,
        config=PERIODIC_PASS_CONFIGS["health"],
        settings=settings,
    )
```

The `Settings` import is kept (even if unused directly) as a
monkeypatch seam for tests.

---

## Step 4 — Add config defaults

Edit `config/config.example.json`.  You need two entries:

1. **Model default** — in the `core.models` block, add the model
   identifier that the `${MILL_<NAME>_MODEL}` env-var reference
   resolves to:
   ```yaml
   health: deepseek/deepseek-v4-flash
   ```

2. **Per-agent block** — add a dedicated section with enabled flag and
   interval.  Example (from `bc_check`):
   ```yaml
   health:
     model: deepseek/deepseek-v4-flash
     enabled: true
     interval_seconds: 86400
   ```

   The memory ledger path is not configurable — it is fixed to
   `<data_dir>/<repo_id>/<name>_memory.md`.

   The `model` sub-key here is the agent-specific model override; the
   `core.models` entry is the default that the env-var reference
   resolves to when no override is set.

---

## Step 5 — Wire the worker

Open `src/robotsix_mill/runtime/worker/core.py`.

### 5a — Declare the task attribute

Add a task attribute in the `__init__` block (alphabetically among
the existing ~15 declarations):

```python
self._health_task: asyncio.Task | None = None
```

### 5b — Wire the poll loop (periodic agents)

If your agent runs on a timer, add a `_start_poll_loop_pass` call in
the `start()` method:

```python
self._start_poll_loop_pass(
    "health",
    self._health_poll_loop,
    "_health_task",
    log_msg="Periodic health enabled: interval %ds",
    log_args=(self.ctx.settings.health_interval_seconds,),
)
```

The poll-loop method itself lives in `poll_loops.py`; wire it there
following the existing patterns (e.g. `_trace_health_poll_loop`).

### 5c — Cancel on shutdown

Add `"_health_task"` to the shutdown task-cancellation list in
`stop()`.

### Alternative: HTTP-triggered background pass

Some periodic agents (like `bc_check`) run **only** on HTTP trigger
(POST), not on a timer.  For those, skip 5b and instead wire a route
in Step 6.  You still need the task attribute (5a) for lifecycle
tracking and the cancellation entry (5c).

---

## Step 6 — CLI and API route (optional)

If the agent should be invocable on-demand via the management API:

1. Open `src/robotsix_mill/runtime/routes/_passes.py`.

2. Add a `_make_background_pass` call and register it with the router:

   ```python
   health_check_pass = _make_background_pass(
       kind="health",
       runner_module="robotsix_mill.runners.health_runner",
       runner_func="run_health_pass",
       docstring="""Kick off a codebase-health pass in the
       BACKGROUND and return at once. …""",
   )
   router.post("/health-check", status_code=202)(health_check_pass)
   ```

3. If you also want a CLI entry-point, add it to the management CLI
   under `src/robotsix_mill/cli/` following the existing pattern.

---

## Step 7 — Testing

Create `tests/agents/test_<name>.py`.  The canonical pattern
monkeypatches `run_<name>_agent` so the test never calls a real LLM.
Example from `tests/agents/test_health.py`:

```python
import pytest
from unittest.mock import Mock, call

from robotsix_mill.agents import health as health_agent
from robotsix_mill.config import Settings
from robotsix_mill.runners.health_runner import run_health_pass


@pytest.fixture
def _mock_health(monkeypatch):
    """Replace the real agent with a no-op mock."""
    mock = Mock(spec=health_agent.run_health_agent)
    mock.return_value = health_agent.HealthResult(
        summary="All clear.",
        updated_memory="",
        draft_titles=[],
        draft_bodies=[],
        gap_ids=[],
    )
    monkeypatch.setattr(health_agent, "run_health_agent", mock)
    return mock


def test_run_health_pass(tmp_path, _mock_health):
    settings = Settings(data_dir=tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.runners.health_runner.Settings",
        lambda: settings,
    )
    result = run_health_pass(
        session_id="test-sid",
        repo_config=_test_repo_config(),
    )
    _mock_health.assert_called_once()
    assert result.pass_summary == "All clear."
```

Key points:

- Monkeypatch **at the agent module level** (`health_agent.run_health_agent`),
  not at the runner level — the runner imports the agent function and
  calls it directly.
- Monkeypatch `Settings` in the **runner module** so the runner stub
  picks up your test settings (especially `data_dir`).
- Assert that the mock was called and inspect its return value.
- The test must **never** reach a real model call — if it does, the
  hermetic test gate will fail.

---

## Checklist

- [ ] `SourceKind` member added in `src/robotsix_mill/core/models.py`
- [ ] YAML definition at `agent_definitions/periodic/<name>.yaml`
- [ ] Python module at `src/robotsix_mill/agents/<name>.py`
- [ ] `PERIODIC_PASS_CONFIGS` entry in `src/robotsix_mill/runners/periodic_runner.py`
- [ ] Result dataclass alias (e.g. `HealthPassResult`) in `periodic_runner.py`
- [ ] Thin runner stub at `src/robotsix_mill/runners/<name>_runner.py`
- [ ] Config defaults in `config/config.example.json` (model + per-agent block)
- [ ] Task attribute in `src/robotsix_mill/runtime/worker/core.py`
- [ ] Poll-loop wiring (periodic) **or** HTTP route (`_passes.py`)
- [ ] Shutdown cancellation entry in `core.py`
- [ ] Test file at `tests/agents/test_<name>.py`
- [ ] `docs/modules.yaml` updated if new files introduce a new module
