# Board Agent — agent-comm bridge to the mill board API

The board agent is an opt-in service that bridges
[agent-comm](https://github.com/damien-robotsix/robotsix-agent-comm)
messages to the mill board REST API. Other agents can drive the board
programmatically — query, file, comment, transition, approve, merge,
resume, migrate — instead of calling the HTTP API directly.

## Quick start

1. **Enable** the agent:

   ```bash
   export MILL_BOARD_AGENT_ENABLED=true
   export MILL_BOARD_AGENT_REPO_ID=my-board
   export MILL_BOARD_AGENT_API_TOKEN=mill-api-token
   ```

   Or in `config/mill.local.yaml`:

   ```yaml
   board_agent_enabled: true
   board_agent_api_url: "http://localhost:8000"
   board_agent_api_token: "mill-api-token"
   board_agent_repo_id: "my-board"
   ```

2. **Start the mill** — the board agent starts alongside the worker
   and registers itself with the shared agent-comm `Registry`.

3. **Send a structured operation** via agent-comm:

   ```json
   {
     "op": "list_tickets",
     "args": {"state": "open"}
   }
   ```

## Configuration reference

| Field | Env var | Default | Description |
|-------|---------|---------|-------------|
| `board_agent_enabled` | `MILL_BOARD_AGENT_ENABLED` | `false` | Enable the board agent service |
| `board_agent_api_url` | `MILL_BOARD_AGENT_API_URL` | `http://localhost:8000` | Mill board API base URL |
| `board_agent_api_token` | `MILL_BOARD_AGENT_API_TOKEN` | `""` | API token for authenticating to the board |
| `board_agent_repo_id` | `MILL_BOARD_AGENT_REPO_ID` | `""` | Board/repo id scoping the agent's operations |
| `board_agent_write_ops` | `MILL_BOARD_AGENT_WRITE_OPS` | `true` | Gate for write operations (create, comment, transition, etc.) |

## Write-ops gate

When `board_agent_write_ops` is `false`, any write operation
(`create_ticket`, `comment`, `transition`, `approve`, `mark_done`,
`merge_now`, `resume_blocked`, `migrate`, `set_priority`) returns an
`Error` response with a clear message. Read operations
(`list_tickets`, `get_ticket`, `board_cards`, `history`,
`merge_status`, `description`) are unaffected.

This lets a deployment run the agent in read-only mode for
observability without risking autonomous board mutation.

## Available operations

### Read

| Op | Args | Description |
|----|------|-------------|
| `list_tickets` | `state`, `repo` filters | List/search tickets |
| `get_ticket` | `ticket_id` | Get full ticket details |
| `board_cards` | — | Board card columns |
| `history` | `ticket_id` | Ticket event history |
| `merge_status` | `ticket_id` | Merge readiness check |
| `description` | `ticket_id` | Ticket description body |

### Write (gated by `board_agent_write_ops`)

| Op | Args | Description |
|----|------|-------------|
| `create_ticket` | `title`, `description`, … | File a new ticket |
| `comment` | `ticket_id`, `body` | Post a comment |
| `transition` | `ticket_id`, `state` | Move ticket to a state |
| `approve` | `ticket_id` | Approve a ticket |
| `mark_done` | `ticket_id` | Mark ticket done |
| `merge_now` | `ticket_id` | Trigger merge |
| `resume_blocked` | `ticket_id` | Resume a blocked ticket |
| `migrate` | `ticket_id`, `target_repo` | Migrate ticket to another repo |
| `set_priority` | `ticket_id`, `priority` | Set ticket priority |

## Lifecycle

- **Startup**: When `board_agent_enabled` is `true`, the mill lifespan
  imports `robotsix_board_agent.BoardAgent`, creates a shared
  agent-comm `Registry`, constructs the agent, and calls
  `await agent.start()`.
- **Runtime**: The agent listens on the agent-comm transport and
  dispatches structured ops to the board REST API.
- **Shutdown**: The lifespan calls `await agent.stop()` before
  stopping the worker.

When `board_agent_enabled` is `false` (the default), the
`robotsix_board_agent` package is never imported — zero overhead.

## Sending ops from another agent

Other agents discover the board agent via the shared agent-comm
`Registry` and send a `Request` with a structured body:

```python
response = await registry.request(
    target="board-<repo_id>",
    body={"op": "get_ticket", "args": {"ticket_id": "20250331T142030Z-fix-auth-a3f2"}},
)
```

The board agent returns a `Response` with the result payload, or an
`Error` for unknown ops, API failures, or write ops when the gate is
disabled.
