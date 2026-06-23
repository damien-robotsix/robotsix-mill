---
name: board-report
---

## Board: filing draft tickets

### `report_issue` — file a draft ticket

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
