# Agent YAML Schema Specification

Each agent definition lives in its own `.yaml` file under
`agent_definitions/` (e.g. `agent_definitions/refine.yaml`,
`agent_definitions/audit.yaml`). This document specifies every field
a YAML file may contain, its type, default, constraints, and
semantics. It is the contract that the loader (ticket 2), migration
(ticket 3), and runtime (ticket 4) implement against.

## Design rules

- **Required fields are minimal.** Only `name`, `description`,
  `system_prompt`, and `category` are mandatory — everything else has
  a sensible default.
- **Unknown top-level keys MUST be ignored** by consumers. The loader
  must not reject a file because it contains a key it doesn't
  understand. This guarantees forward compatibility: new optional
  fields can be added at any time, and existing YAML files remain
  valid.
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

### `description` (required)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | **yes** |
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
`tools` + `web` + `report_issue`) before passing it to the model.

Use YAML block-scalar syntax (`|` or `|-`) for readability.

---

### `category` (required)

| Attribute | Value |
|-----------|-------|
| Type | `string` (enum) |
| Required | **yes** |
| Valid values | `"pipeline"`, `"periodic"`, `"sub_agent"` |

Which class of agent this is:

- **`pipeline`** — an agent invoked as a stage in the ticket-processing
  pipeline (refine, implement, review, test, rebase, ci_fix, document,
  epic_breakdown, epic_status, dedup, answer, agent_check).
- **`periodic`** — an agent run on a schedule or as a background task
  (audit, health, survey, retrospect, test_gap).
- **`sub_agent`** — a utility agent called by other agents as a tool
  (explore, web_research, trace_inspector).

---

### `model` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | no |
| Default | `"deepseek/deepseek-v4-pro"` (the coordinator `model` field) |

The OpenRouter model identifier. Supports two forms:

1. **Literal model name:**
   ```yaml
   model: "openai/gpt-4o-mini"
   ```
   Used directly as the model argument.

2. **Environment-variable reference:**
   ```yaml
   model: "${MILL_REFINE_MODEL}"
   ```
   The loader resolves this to a `Settings` field whose env alias
   matches the name inside `${…}`. The mapping from `${NAME}` to
   `Settings` field is:

   | `${…}` variable | Settings field | Example default |
   |---|---|---|
   | `${MILL_MODEL}` | `model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_EXPLORE_MODEL}` | `explore_model` | `"deepseek/deepseek-v4-flash"` |
   | `${MILL_TEST_MODEL}` | `test_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_REFINE_MODEL}` | `refine_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_ANSWER_MODEL}` | `answer_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_RETROSPECT_MODEL}` | `retrospect_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_AUDIT_MODEL}` | `audit_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_DEDUP_MODEL}` | `dedup_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_TRIAGE_MODEL}` | `triage_model` | `"openai/gpt-4o-mini"` |
   | `${MILL_WEB_RESEARCH_MODEL}` | `web_research_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_AUTO_APPROVE_MODEL}` | `auto_approve_model` | `"openai/gpt-4o-mini"` |
   | `${MILL_REVIEW_MODEL}` | `review_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_DOC_MODEL}` | `doc_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_TRACE_INSPECTOR_MODEL}` | `trace_inspector_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_TEST_GAP_MODEL}` | `test_gap_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_AGENT_CHECK_MODEL}` | `agent_check_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_HEALTH_MODEL}` | `health_model` | `"deepseek/deepseek-v4-pro"` |
   | `${MILL_SURVEY_MODEL}` | `survey_model` | `"deepseek/deepseek-v4-pro"` |

The `model` field is optional. When absent, the loader falls back to
`${MILL_MODEL}` (the coordinator default).

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
| `read_file` | fs | Return the text content of a file in the repository |
| `write_file` | fs | Create or overwrite a file in the repository |
| `edit_file` | fs | Replace a unique string in a file |
| `delete_file` | fs | Delete a file from the repository |
| `list_dir` | fs | List entries of a directory in the repository |
| `run_command` | shell | Run a shell command against the repository |

When `tools` is absent or empty, the agent receives no tools. Some
agents are "classification-only" (triage, auto-approve) and use no
tools.

---

### `web` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `boolean` |
| Required | no |
| Default | `false` |

Whether the `web_research` sub-agent tool is injected into the agent's
tool set. When `true`, the loader appends `web_research` (a cheap
sub-agent that performs external web lookups and returns a conclusion
to the parent). This is NOT the OpenRouter `:online` mode — it is a
dedicated tool that isolates web search cost and latency from the
primary model call.

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
`epic_status`) set this to `false` to avoid double-reporting.

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
`skills/<name>/SKILL.md` file that the loader reads and appends to the
system prompt. For example:

```yaml
skills:
  - "python-testing"
  - "git-workflow"
```

This field is **forward-looking**: no skill-loading code exists today.
It is included in the schema from day one so that YAML files written
now do not need to be updated when skill loading is implemented.
Consumers that do not implement skill loading should silently ignore
this field.

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

## Complete example

See `agent_definitions/refine.yaml` for a fully-worked example of the
refine agent — the most feature-rich agent in the system. It
demonstrates:

- A multi-line `system_prompt` using YAML block-scalar syntax
- Environment-variable model reference (`${MILL_REFINE_MODEL}`)
- A tool list (`explore`, `read_file`, `list_dir`, `run_command`)
- `web: true` and `report_issue: true` (the defaults for pipeline agents)
- `output_type: RefineResult` and `retries: 2`
- `category: pipeline`
- `module: refining` — explicit module path
- `skills: []` — placeholder for forward compatibility

## Extensibility guarantee

1. **New optional fields** can be added to the schema at any time.
   Existing YAML files remain valid because they simply don't contain
   the new key, and the loader uses the documented default.

2. **Unknown top-level keys** encountered by the loader must be
   silently ignored, not rejected. This allows YAML files authored
   against a newer schema to be loaded by an older loader (provided
   the older loader doesn't depend on the new field's semantics).

3. **New required fields** must never be added. The set of required
   fields is permanently `name`, `description`, `system_prompt`,
   `category`. Any new concept must be optional with a sensible
   default.
