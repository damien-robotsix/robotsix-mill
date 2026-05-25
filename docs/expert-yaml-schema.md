# Expert YAML Schema Specification

Each expert domain definition lives in its own `.yaml` file under
`expert_definitions/` (e.g. `expert_definitions/python-backend.yaml`).
This document specifies every field a YAML file may contain, its type,
default, constraints, and semantics. It is the contract that the
loader, `ExpertManager`, and `Coordinator` implement against.

## Design rules

- **Required fields are minimal.** Only `domain`, `module_paths`, and
  `system_prompt` are mandatory — everything else has a sensible
  default.
- **Unknown top-level keys are rejected.** The loader uses strict
  validation (`extra="forbid"`) — a YAML file containing a key not
  defined in the schema will fail to load. This catches typos and
  drift early. When adding a new field, update the `ExpertDefinition`
  model in `expert_loader.py` first, then the schema doc, then the
  YAML files.
- **One file per domain.** Each expert occupies its own
  `expert_definitions/<domain>.yaml`. This keeps diffs isolated and
  makes the directory human-browsable.
- **Flat and declarative.** The schema mirrors the `ExpertDefinition`
  Pydantic model directly — no abstract type hierarchies, no DSL,
  no deep nesting beyond the `memory` sub-config.

## Field reference

### `domain` (required)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | **yes** |
| Valid pattern | `/^[a-z0-9]+(?:-[a-z0-9]+)*$/` |
| Example | `"python-backend"`, `"docs"`, `"frontend"` |

A unique, slug-like identifier for the expert domain. Must contain
only lowercase letters, digits, and single hyphens between segments.
This is the canonical name used in logs, metrics, and the expert's
memory ledger filename (`<data_dir>/expert_<domain>_memory.md`).

---

### `description` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | no |
| Default | `null` |
| Example | `"Expert covering the Python backend source code under src/robotsix_mill/"` |

A human-readable summary of the expert's domain. Used in expert
listings, audit output, and documentation.

---

### `module_paths` (required)

| Attribute | Value |
|-----------|-------|
| Type | `list[string]` |
| Required | **yes** |
| Example | `["src/**/*.py"]`, `["docs/", "mkdocs.yml"]` |

One or more file/directory path globs that define the expert's scope.
Each entry must be a non-empty string. The `ExpertManager` uses these
globs to build a scoped `repo_map` index so the expert only sees
relevant files. Glob syntax follows Python `pathlib` conventions — use
`**` for recursive matching.

---

### `system_prompt` (required)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | **yes** |
| Example | (multi-line string — see `expert_definitions/python-backend.yaml`) |

Custom instructions injected into the expert's system prompt. This is
the core prompt text that defines the expert's persona, domain
knowledge boundaries, and behavioural constraints.

Use YAML block-scalar syntax (`|` or `|-`) for readability.

---

### `model` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `string` |
| Required | no |
| Default | `""` (empty string — use global expert model) |
| Example | `"anthropic/claude-sonnet-4"`, `"${MY_EXPERT_MODEL}"` |

The OpenRouter model identifier for this expert. Supports two forms:

1. **Literal model name:**
   ```yaml
   model: "anthropic/claude-sonnet-4"
   ```
   Used directly as the model argument.

2. **Environment-variable reference:**
   ```yaml
   model: "${MY_EXPERT_MODEL}"
   ```
   The loader resolves `${VAR}` placeholders against `os.environ`.
   Unset variables resolve to an empty string.

When `model` is empty (the default), the `ExpertManager` falls back to
the global expert model (configured separately in `Settings`).

---

### `memory` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `ExpertMemoryConfig` (sub-model) |
| Required | no |
| Default | `ExpertMemoryConfig()` (all sub-field defaults applied) |

Memory/retrieval tuning for this expert. Controls how much context the
expert loads and how the `repo_map` retriever splits and returns
results. See the [Memory sub-config](#memory-sub-config) section below.

---

### `skills` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `list[string]` |
| Required | no |
| Default | `[]` (empty list) |

Skill doc names to inject into the expert's prompt. Each entry
references a `skills/<name>/SKILL.md` file whose Markdown body is
injected between the system prompt and the tool table. Follows the
same convention as `AgentDefinition.skills`. For example:

```yaml
skills:
  - board
```

---

### `tools` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `list[string]` |
| Required | no |
| Default | `["explore", "read_file", "list_dir"]` |

Tool allow-list for the expert. Each string must match a registered
tool name. The default is a read-only exploration set — no
`run_command`, `write_file`, or `edit_file`. Add tools explicitly
when the expert needs them:

```yaml
tools:
  - explore
  - read_file
  - list_dir
  - run_command
```

---

### `extras` (optional)

| Attribute | Value |
|-----------|-------|
| Type | `dict[string, any]` |
| Required | no |
| Default | `{}` (empty dict) |

Extension point for arbitrary key-value pairs that downstream code
(`ExpertManager`, `Coordinator`) can consume without schema changes.
Unknown top-level keys are rejected (see [Design rules](#design-rules)) —
use `extras` for deliberate passthrough:

```yaml
extras:
  custom_key: custom_value
  priority: high
```

---

## Memory sub-config

The `memory` field accepts an `ExpertMemoryConfig` sub-model with the
following fields. All fields are optional with sensible defaults.

### `max_memory_chars`

| Attribute | Value |
|-----------|-------|
| Type | `int` |
| Required | no |
| Default | `8000` |

Maximum characters loaded into the expert's memory ledger per
invocation. Controls the context window budget for memory retrieval.

### `chunk_size`

| Attribute | Value |
|-----------|-------|
| Type | `int` |
| Required | no |
| Default | `2000` |

Approximate character chunk size for `repo_map` splitting. Smaller
chunks produce finer-grained retrieval at the cost of more chunks.

### `max_chunks`

| Attribute | Value |
|-----------|-------|
| Type | `int` |
| Required | no |
| Default | `20` |

Maximum chunks the `repo_map` retriever emits per query. Caps the
total context expansion from repository retrieval.

### `memory_path`

| Attribute | Value |
|-----------|-------|
| Type | `string` or `null` |
| Required | no |
| Default | `null` |

Explicit path to the expert's memory ledger file. When `null`, the
`ExpertManager` derives the path as
`<data_dir>/expert_<domain>_memory.md` at runtime.

### `extras`

| Attribute | Value |
|-----------|-------|
| Type | `dict[string, any]` |
| Required | no |
| Default | `{}` (empty dict) |

Extension point for retriever-specific tuning (e.g. embedding model,
similarity threshold).

---

## Complete example

See `expert_definitions/python-backend.yaml` for a fully-worked example
of a Python backend expert. It demonstrates:

- A unique slug-like `domain` identifier
- A multi-line `system_prompt` using YAML block-scalar syntax
- `module_paths` scoped to `src/**/*.py`
- Empty `model` (falls back to global expert model)
- Full `memory` sub-config with all defaults shown explicitly
- `skills: [board]` — the board interaction skill
- `tools` including `run_command` for execution capability
- Empty `extras` dict as a placeholder

## Extensibility guarantee

1. **New optional fields** can be added to the schema at any time.
   Existing YAML files remain valid because they simply don't contain
   the new key, and the loader uses the documented default.

2. **Unknown top-level keys** are rejected by the strict loader
   (`extra="forbid"` on both `ExpertDefinition` and
   `ExpertMemoryConfig`). When adding a new field, update the Pydantic
   model in `expert_loader.py` first, then the schema doc, then the
   YAML files. This catches typos and drift early.

3. **New required fields** must never be added. The set of required
   fields is permanently `domain`, `module_paths`, `system_prompt`.
   Any new concept must be optional with a sensible default.

4. **`extras` is the escape hatch.** When downstream code needs to
   consume keys that aren't in the schema yet, put them in `extras`.
   Once a key stabilises, promote it to a top-level field with a
   default — existing YAML files that use `extras` continue to work
   (the promoted field takes priority, and the `extras` copy is
   ignored).
