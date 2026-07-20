# Agent YAML Schema Specification

Each agent definition lives in its own `.yaml` file under
`agent_definitions/` (e.g. `agent_definitions/refine.yaml`,
`agent_definitions/audit.yaml`). This document specifies every field
a YAML file may contain, its type, default, constraints, and
semantics. It is the contract that the loader, migration, and runtime
implement against.

## Design rules

- **Required fields are minimal.** Only `name`, `level`, and
  `system_prompt` are mandatory — everything else has a sensible
  default.
- **Unknown top-level keys are rejected.** The loader uses strict
  validation (``extra="forbid"``) — a YAML file containing a key not
  defined in the schema will fail to load. This catches typos and
  drift early. When adding a new field, update the ``AgentDefinition``
  model in ``yaml_loader.py`` first, then the schema doc, then the
  YAML files.
- **One file per agent.** Each agent occupies its own
  `agent_definitions/<name>.yaml`. This keeps diffs isolated and
  makes the directory human-browsable.
- **Flat and declarative.** The schema mirrors the `build_agent()`
  parameter surface directly — no abstract type hierarchies, no DSL,
  no deep nesting.

## Field reference

### `name` (required)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | **yes** |
| Example | `"refine"`, `"audit"`, `"agent_check"` |

A unique, short identifier for the agent. This is the canonical name
used in logs, metrics, and tool attribution. It must be unique across
all loaded agent definitions. Convention: lowercase, snake_case.

---

### `description` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | no |
| Default | `null` |
| Example | `"Refines a rough ticket draft into a precise, self-contained engineering spec grounded in the actual codebase"` |

A one-line human-readable summary of the agent's role. Used in agent
listings, audit output, and documentation.

---

### `system_prompt` (required)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | **yes** |
| Example | (multi-line string — see `agent_definitions/refine.yaml`) |

The core prompt text that defines the agent's behaviour, persona,
constraints, and output format. This is the full prompt text — the
loader concatenates it with a tool-use appendix (generated from
`tools` + `web_knowledge` + `report_issue`) before passing it to the model.

Use YAML block-scalar syntax (`|` or `|-`) for readability.

---

### `category` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `string` (enum) |
| Required | no |
| Default | `null` |
| Valid values | `"pipeline"`, `"periodic"`, `"sub_agent"`, `"interactive"`, `"sandboxed"` |

Which class of agent this is:

- **`pipeline`** — an agent invoked as a stage in the ticket-processing
  pipeline (refine, implement, review, triage, document, retrospect,
  dedup, epic_breakdown, obsolescence, auto-approve,
  scope_triage, spec-review, run_tests, doc_classifier,
  reviewer-agreement, test_scope, pipeline/meta_triage).
- **`periodic`** — an agent run on a schedule or as a background task
  (audit, health, survey, test_gap, agent_check, epic_status, bc_check,
  completeness_check, copy_paste, diagnostic, docstring_coverage,
  forge_parity, frontend_sync, meta, module_size, module_curator,
  repo_description_sync, run_health, state_sync,
  triage_boilerplate).
- **`sub_agent`** — a utility agent called by other agents as a tool
  (codeql_fp_triage).
- **`sandboxed`** — an agent that executes in an ephemeral sandbox
  (ci_fix, rebase, review_revision).
- **`interactive`** — a prompt-to-ticket or Q&A agent triggered by user
  interaction (answer).

---

### `level` (required)

| Attribute | Value |
|-----------|-------|
| Type | `integer` |
| Required | **yes** |
| Constraints | `ge=1, le=3` |
| Example | `level: 2` |

The agent's capability tier, used by `build_agent` to resolve a
concrete `(transport, model)` pair via llmio's tier defaults. Three
levels are defined:

| Level | Label | Resolved model |
|-------|-------|----------------|
| 1 | flash | Cheapest, fastest model (DeepSeek flash or equivalent) |
| 2 | pro   | Balanced cost/capability (DeepSeek pro or equivalent) |
| 3 | opus  | Most capable model (Claude opus or equivalent) |

The mapping from `level` to concrete model is **not** defined in
the YAML file — it lives in `Settings` and the tier-resolution
layer. Agent authors choose a capability tier; operators configure
which providers serve each tier.

Every agent YAML must specify a `level`.  There is no default —
omitting it is a validation error.

---

### `tools` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `list[string]` |
| Required | no |
| Default | `[]` (empty list) |

Tool names the agent receives at construction time. Each string must
match the `__name__` of a tool function registered in the
`ToolRegistry`. Valid values today:

| Tool name | Category | Description |
|-----------|----------|-------------|
| `explore` | exploration | Ask a sub-agent a complex, multi-step question about the repository |
| `parallel_explore` | exploration | Run multiple `explore` sub-agents in parallel |
| `read_file` | fs | Return the text content of a file in the repository |
| `write_file` | fs | Create or overwrite a file in the repository |
| `edit_file` | fs | Replace a unique string in a file |
| `delete_file` | fs | Delete a file from the repository |
| `list_dir` | fs | List entries of a directory in the repository |
| `run_command` | shell | Run a shell command against the repository |
| `validate_artifact` | quality | Validate a generated artifact (schema check, lint) |
| `detect_duplication` | quality | Detect copy-paste duplication across files |
| `langfuse_session_summary` | observability | Summarize a Langfuse session |
| `langfuse_list_traces` | observability | List traces in a Langfuse session |
| `langfuse_trace_detail` | observability | Fetch detail for a single Langfuse trace |
| `langfuse_session_cost` | observability | Fetch cost for a Langfuse session |
| `langfuse_inspect_trace` | observability | Inspect a Langfuse trace for anomalies |
| `inspect_cost` | observability | Inspect token cost for a run |
| `query_app_logs` | observability | Query application logs |
| `git_fetch` | vcs | Fetch from a git remote |
| `git_remote_sha` | vcs | Resolve a remote ref to a commit SHA |
| `git_push_with_lease` | vcs | Push with lease safety check |
| `git_branch_ancestry` | vcs | Check branch ancestry relationship |
| `wait_for_ci` | ci | Wait for CI checks to complete |
| `fetch_ci_logs` | ci | Fetch CI run logs |
| `read_ticket` | tickets | Fetch full ticket details by ID |

This table is illustrative, not exhaustive. The authoritative
registry of available tools is the `ToolRegistry` wiring in
`src/robotsix_mill/agents/base.py` and the individual tool modules
under `src/robotsix_mill/agents/`.

When `tools` is absent or empty, the agent receives no tools. Some
agents are "classification-only" (triage, auto-approve) and use no
tools.

---

### `web_knowledge` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `false` |

Whether the `ask_web_knowledge` multi-turn sub-agent is injected into
the agent's tool set. When `true`, the loader appends
`ask_web_knowledge` — a flash-tier sub-agent that owns a per-repo
Markdown knowledge base AND a web-search tool, and decides
autonomously which source to consult.

This is the single gateway for all external knowledge: library docs,
web searches, and cached lookups all flow through this one tool so
that cost attribution stays tractable and the knowledge base
accumulates instead of fragmenting. The old `web` flag (direct
`web_research` injection) is gone.

---

### `report_issue` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `true` |

Whether the `report_issue` self-reporting tool is injected. When
`true`, the agent can file a draft ticket for blocking issues
(missing tool, error, workflow gap). When `false`, the tool is
omitted. Agents that already emit structured draft tickets through
their `output_type` (`audit`, `retrospect`, `health`, `survey`,
`test_gap`, `agent_check`, `document`, `epic_breakdown`,
`epic_status`, `completeness_check`) set this to `false` to avoid double-reporting.

---

### `read_ticket` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `false` |

Whether the `read_ticket` read-only tool is injected. When `true`, the
agent can fetch the full details (description, history, and comments)
of any ticket by ID. This is the safe, read-only counterpart to
`report_issue` — same wiring, opposite direction.

Periodic agents (`audit`, `health`, `survey`, `test_gap`, `bc_check`,
`agent_check`, `completeness_check`, `copy_paste`, `docstring_coverage`,
`forge_parity`, `frontend_sync`, `meta`, `module_curator`, `module_size`, `run_health`,
`state_sync`, `triage_boilerplate`) set this to `true` so they
can look up the full context of past proposals when the one-line
summary in `<recent_proposals>` isn't enough. Pipeline agents and
other on-demand agents typically leave this `false`.

---

### `list_epic_children` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `false` |

Whether the `list_epic_children` read-only tool is injected. When
`true`, the agent can list the sibling epic children of the current
ticket — useful for agents that need to check whether a ticket
belongs to an epic and what other children exist.

---

### `reply_to_thread` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `true` |

Whether the `reply_to_thread` replying tool is injected. When `true`, the
agent can reply to a comment thread on the current ticket, enabling
conversation with humans through threaded comments. When `false`, the
tool is omitted.

Pipeline agents that interact with human reviewers (`implement`) keep
this at the default `true`. Agents that produce one-shot structured
output (`review`, `refine`, `audit`) set this to `false` since they
don't participate in ongoing conversations.

---

### `close_thread` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `true` |

Whether the `close_thread` closing tool is injected. When `true`, the
agent can close (resolve) a top-level comment thread on the current
ticket after addressing the feedback it contains. When `false`, the
tool is omitted.

Pipeline agents that address human reviewer feedback (`implement`)
keep this at the default `true`. Agents that produce one-shot
structured output (`review`, `refine`, `audit`) set this to `false`
since they don't need to mark threads as resolved.

---

### `list_threads` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `true` |

Whether the `list_threads` read-only tool is injected. When `true`,
the agent can list top-level comment threads on the current ticket
with open/closed status. Pipeline agents that interact with human
reviewers keep this at the default `true`.

---

### `ask_user` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `true` |

Whether the `ask_user` tool is injected. When `true`, the agent can
pause the current ticket and ask the operator a clarifying question.
Agents that must never block on human input (e.g. fully automated
periodic agents) set this to `false`.

---

### `output_type` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | no |
| Default | absent (agent returns free-form `str`) |

The Pydantic model class name for agents with structured output. The
loader maps this to the actual Pydantic model class defined in the
agent's Python module (e.g., `"RefineResult"` → `RefineResult` model
from `src/robotsix_mill/agents/refining.py`).

When `output_type` is absent, the agent returns free-form strings
(`str`). When present, the loader wraps the model in `PromptedOutput`
(for models that reject forced `tool_choice`).

**Convention:** the string value matches the Python class name
exactly. The loader is responsible for importing the model class from
the agent's module.

---

### `retries` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `integer` |
| Required | no |
| Default | `2` |
| Constraints | `>= 0` |

The number of output-retry attempts pydantic-ai performs when
structured output validation fails. Set to `0` to disable retries.

---

### `skills` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `list[string]` |
| Required | no |
| Default | `[]` (empty list) |

Skill names to inject into the agent's prompt. Each entry references a
`skills/<name>/SKILL.md` file whose Markdown body (with YAML frontmatter
stripped) is injected under a `## Skills` heading, between the system
prompt and the `## Available tools` table. For example:

```yaml
skills:
  - board-read
```

Three skills are shipped today under `skills/`:

- **`ask_user_guardrails`** (`skills/ask_user_guardrails/SKILL.md`) —
  guidance on when to ask the operator for help (good reasons vs.
  bad reasons, refine- and implement-specific examples).

- **`board-read`** (`skills/board-read/SKILL.md`) — guidance on reading
  tickets (`read_ticket` tool usage) and the execution-tool preference
  (CLI outside sandboxes, dedicated tool inside sandboxes).

- **`board-report`** (`skills/board-report/SKILL.md`) — guidance on filing
  draft tickets (`report_issue` tool usage, dedup guard, evidence param),
  with the same execution-tool preference pattern.

Additional skills can be added by creating a `skills/<name>/SKILL.md`
file with a `name:` frontmatter key.

If a skill file is missing, the factory logs a warning and continues
(no crash).

---

### `modules` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `false` |

Whether the agent receives a `## Module Map` section in its composed
system prompt, derived from `docs/modules.yaml` (the canonical module
taxonomy). When `true`, `compose_prompt` reads `docs/modules.yaml` and
renders a scannable block with one sub-heading per module, its
description, file paths, and dependency hints.

If the taxonomy exceeds 20 modules, only top-level (foundational)
modules are rendered with a pointer to `docs/modules.yaml` for the
complete list.

Agents that do not set `modules: true` (or omit the field) receive an
unchanged prompt — no `## Module Map` section is injected.

Example:

```yaml
modules: true
```

---

### `inject_agent_md` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `true` |

Whether `AGENT.md` (the repo's agent instructions document) is
injected into the system prompt as a `## Repository Conventions`
block. When `true`, every agent receives the repo's AGENT.md content
automatically. Agents that operate outside a repo context (e.g.
meta, answer) set this to `false`.

---

### `inject_language_conventions` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `false` |

Opt-in flag that injects the repo's `## Language conventions` block
(resolved via `resolve_language_instructions`) into the system prompt
when a `repo_dir` is available. The refine and implement stages inject
these themselves; this flag wires the same conventions into
review-type agents (retrospect, review, audit) so they don't misjudge
valid version-specific syntax (e.g. PEP-758 `except A, B:` on
Python 3.14).

---

### `max_tokens` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `integer` or `null` |
| Required | no |
| Default | `null` |

An explicit cap on model output tokens. When `null` (the default),
the model's own default applies. Set to a positive integer to
constrain long responses (useful for classification-only agents
like triage or auto-approve).

---

### `module` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | no |
| Default | (derived from `name` if absent) |

The Python module path (relative to `src/robotsix_mill/agents/`) that
contains the agent's Pydantic output model and any agent-specific
utilities. For example, the refine agent lives in `refining.py`, so
its `module` would be `"refining"`. The loader uses this to import the
`output_type` class when `output_type` is set.

When absent, the loader derives it from `name` by convention (e.g.
`name: "refine"` → module `"refining"`; `name: "audit"` → module
`"auditing"`). The mapping is not always mechanical (e.g. `agent_check`
→ `agent_check.py`), so explicit is better when the convention
doesn't hold.

---

### `interval` (optional, periodic agents only)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | no |
| Default | `null` |
| Valid forms | `"1w"`, `"1d"`, `"2h30m"`, `"1w2d3h40m10s"` |
| Mutually exclusive with | `interval_seconds` |

The preferred human-readable form for specifying periodic agent run
intervals. Accepts descending-order duration syntax with units for
weeks (`w`), days (`d`), hours (`h`), minutes (`m`), and seconds (`s`).
Each unit appears at most once. Examples:

- `interval: "1d"` — once per day
- `interval: "1w"` — once per week
- `interval: "2h30m"` — every 2.5 hours
- `interval: "1w2d3h40m10s"` — once per 790810 seconds

If both `interval` and `interval_seconds` are set, validation fails.
When `interval` is set, it is parsed to seconds and stored internally,
so downstream code always sees an integer number of seconds.

---

### `interval_seconds` (optional, periodic agents only)

| Attribute | Value |
|-----------|-------|
| Type | `integer` |
| Required | no |
| Default | `null` |
| Mutually exclusive with | `interval` |
| Example | `3600`, `86400`, `604800` |

The legacy form for specifying periodic agent run intervals as a raw
number of seconds. Kept for backward compatibility — new YAML files
should use `interval` (human-readable) instead.

Examples:
- `interval_seconds: 86400` — once per day (equivalent to `interval: "1d"`)
- `interval_seconds: 604800` — once per week (equivalent to `interval: "1w"`)
- `interval_seconds: 3600` — once per hour

If both `interval` and `interval_seconds` are set, validation fails.
When `interval_seconds` is absent, the agent inherits the corresponding
`Settings` field (e.g. `audit_interval_seconds` from config).

---

### `enabled` (optional, periodic agents only)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `null` (inherits from Settings) |

Whether this periodic agent is enabled for the repository. When `true`,
the agent runs on its schedule. When `false`, the agent is disabled.
When `null` (or absent), the agent inherits the corresponding `Settings`
field (e.g. `audit_enabled` from config).

Used in per-repo override files (`.robotsix-mill/periodic/<name>.yaml`)
to enable or disable a periodic agent for a specific repository without
modifying the shipped agent definition.

---

## Complete example

See `agent_definitions/refine.yaml` for a fully-worked example of the
refine agent — the most feature-rich agent in the system. It
demonstrates:

- A multi-line `system_prompt` using YAML block-scalar syntax
- `level: 3` — opus-tier capability
- A tool list (`explore`, `parallel_explore`, `read_file`, `list_dir`, `run_command`, …)
- `web_knowledge: true` and `report_issue: true` (the defaults for pipeline agents)
- `output_type: RefineResult` and `retries: 2`
- `category: pipeline`
- `module: refining` — explicit module path
- `skills: [board-report, ask_user_guardrails]` — the board-report and ask_user_guardrails skills, injected between the system prompt and the tool table

## Extensibility guarantee

1. **New optional fields** can be added to the schema at any time.
   Existing YAML files remain valid because they simply don't contain
   the new key, and the loader uses the documented default.

2. **Unknown top-level keys** are rejected by the strict loader
   (`extra="forbid"`). When adding a new field, update the Pydantic
   model in `yaml_loader.py` first, then the schema doc, then the
   YAML files. This catches typos and drift early.

3. **New required fields** must never be added. The set of required
   fields is permanently `name`, `level`, `system_prompt`. Any new
   concept must be optional with a sensible default.
