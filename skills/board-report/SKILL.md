---
name: board-report
---

## Board: filing draft tickets

### `report_issue` — file a draft ticket

### When to file

- **missing-tool** — a tool you need is not registered
- **error** — a non-recoverable error occurred
- **workflow-improvement** — a gap in the workflow that blocks progress
- **missing-input** — a required input (file, config, credential) is absent
- **code-quality** — a concrete, actionable code-quality problem you
  discovered in files you read that is genuinely out of scope for the
  current ticket. Explain the issue and why it matters (e.g. a function
  that should be split, a missing docstring on a public API, a redundant
  database query).
- **other** — anything else that blocks completion

### Don't file for trivial observations

Do NOT file for:

- Cosmetic observations (whitespace, variable rename preferences)
- Style nits or opinionated formatting preferences
- Non-actionable musings or vague hunches
- A "looks good" / "task complete" signal

When in doubt whether something is genuinely actionable, do NOT file.

A dedup guard prevents spam — filing a ticket with the same title as an
existing open ticket is a no-op. The `evidence` parameter accepts up to
8 KB of supporting text.

### Execution tool preference

When your execution environment allows **network access** to the board
API (e.g. outside a sandbox), prefer `run_command` with CLI calls over
the dedicated Python tools:

- `robotsix-mill ticket new --title '...'` — create a ticket

When running inside a **network-isolated sandbox** (e.g. `--network none`),
fall back to the dedicated `report_issue` tool.
