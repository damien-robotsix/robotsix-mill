---
name: board-read
---

## Board: reading tickets

### `read_ticket` — read ticket details

Use `read_ticket` to fetch the full context of a ticket when a one-line
summary isn't enough. This tool is **read-only** — it cannot modify
tickets. Returns formatted Markdown including the ticket description,
history, and comments (capped at ~6000 characters).

### Execution tool preference

When your execution environment allows **network access** to the board
API (e.g. outside a sandbox), prefer `run_command` with CLI calls over
the dedicated Python tools:

- `robotsix-mill ticket show <id>` — read a ticket

When running inside a **network-isolated sandbox** (e.g. `--network none`),
fall back to the dedicated `read_ticket` tool.
