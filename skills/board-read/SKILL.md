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

### History paging

The `GET /tickets/{id}/history` endpoint supports pagination:

- `?limit=N` — return at most N events (default: unbounded).
- `?offset=N` — skip the first N events (default: 0).
- `?order=asc|desc` — chronological (default) or most-recent-first.

To retrieve the final (most-recent) history event of a ticket whose full
history is too large to read in one response, use:

    GET /tickets/{id}/history?order=desc&limit=1

This returns a single-event response regardless of total history size.
