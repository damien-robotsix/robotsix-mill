# Board Agent

The board agent is an opt-in **agent-comm service** that exposes the mill
board's full ticket lifecycle over structured agent-comm messages.  It
acts as a bridge: other agents (inside or outside this process) send
structured ops, and the board agent translates them into board REST API
calls — querying, filing, commenting, transitioning, approving, merging,
resuming, and migrating tickets — so the board can be driven
programmatically instead of via the HTTP API or a human.

## Enabling

The board agent is **off by default**.  Set these environment variables
(or their YAML equivalents) to enable it:

```bash
export MILL_BOARD_AGENT_ENABLED=true
export MILL_BOARD_AGENT_REPO_ID=my-repo
export MILL_BOARD_AGENT_API_TOKEN=sk-...
# Optional overrides:
# export MILL_BOARD_AGENT_API_URL=http://my-board:8000
# export MILL_BOARD_AGENT_WRITE_OPS=false   # read-only mode
```

| Field | Env var | Default | Description |
|---|---|---|---|
| `board_agent_enabled` | `MILL_BOARD_AGENT_ENABLED` | `false` | Master kill-switch. |
| `board_agent_api_url` | `MILL_BOARD_AGENT_API_URL` | `http://localhost:8000` | Board REST API base URL. |
| `board_agent_api_token` | `MILL_BOARD_AGENT_API_TOKEN` | `""` | Bearer token for board API auth. |
| `board_agent_repo_id` | `MILL_BOARD_AGENT_REPO_ID` | `""` | The `board_id` the agent scopes to (must match a `board_id` in `config/repos.yaml`). |
| `board_agent_write_ops` | `MILL_BOARD_AGENT_WRITE_OPS` | `true` | When `false`, write operations return an Error without hitting the API. |

## Structured ops

Agents communicate with the board agent by sending agent-comm `Request`
messages with an `op` payload:

```json
{
  "op": "<operation-name>",
  "args": { ... }
}
```

The board agent dispatches to the corresponding method on its internal
`BoardClient`, which calls the board REST API.  Responses are returned
as agent-comm `Response` messages (or `Error` on failure).

## Write-op guard

When `board_agent_write_ops` is `false`, any write operation —
`create`, `comment`, `transition`, `approve`, `mark_done`, `merge_now`,
`resume_blocked`, `migrate`, `set_priority` — returns an Error
immediately.  Read operations (`list`, `get`, `board`, `history`,
`merge_status`, `description`) are unaffected.

This provides a **safe read-only mode** for testing or for agents that
should never mutate the board.

## Available operations

The full operation catalog is maintained in the
[robotsix-board-agent](https://github.com/damien-robotsix/robotsix-board-agent) repo.
Broad categories:

**Read operations**
- `list` — list tickets with filters.
- `get` — get a single ticket by id.
- `board` — get the full board state.
- `history` — get a ticket's event history.
- `merge_status` — check the merge status of a ticket.
- `description` — get the description body of a ticket.

**Write operations** *(guarded by `board_agent_write_ops`)*
- `create` — file a new ticket.
- `comment` — post a comment.
- `transition` — move a ticket to a new state.
- `approve` — approve a ticket.
- `mark_done` — mark the current stage done.
- `merge_now` — trigger merge.
- `resume_blocked` — resume a blocked ticket.
- `migrate` — migrate a ticket between repos.
- `set_priority` — change a ticket's priority.

## Lifecycle

The board agent starts and stops with the mill process:

- **Startup** — after the worker is started and unfinished tickets are
  requeued.  The agent registers itself in the process-level
  agent-comm `Registry` as `board-<repo_id>`.
- **Shutdown** — before the worker is stopped, so any agent-comm
  messages in flight are drained before the worker's task pool tears down.

When `board_agent_enabled` is `false` (the default), zero imports from
`robotsix_board_agent` or `robotsix_agent_comm` occur — the `if` guard
short-circuits before the deferred import, so deployments that keep the
agent off pay no import overhead and don't need the package installed.

## Architecture

```text
┌─────────────────────────────┐
│  Other agents (agent-comm)  │
└──────────────┬──────────────┘
               │ Request/Response
               ▼
┌─────────────────────────────┐
│       BoardAgent            │
│  (robotsix-board-agent)     │
│  registered as board-<id>   │
└──────────────┬──────────────┘
               │ httpx
               ▼
┌─────────────────────────────┐
│   Board REST API (mill)     │
│   /api/v1/tickets/...       │
└─────────────────────────────┘
```

The `Registry` is shared at process level (`app.state.agent_registry`)
so future agent-comm consumers (calendar agent, etc.) can reuse the same
in-process message bus.
