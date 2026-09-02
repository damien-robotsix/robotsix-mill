# API Routes

The runtime exposes a REST API and WebSocket endpoints via FastAPI
routers under `runtime/routes/`. All routes are mounted on the main app
in `runtime/api.py`.

## Route modules

| Module | Router tag | Purpose |
|---|---|---|
| `_agents.py` | Agents | Agent definition listing, agent execution |
| `_board.py` | Board | Kanban board WebSocket and column definitions |
| `_candidates.py` | Candidates | Candidate ticket generation from issue trackers |
| `_chat_skill.py` | Chat | Chat skill management |
| `_comments.py` | Comments | Ticket comment CRUD |
| `_epics.py` | Epics | Epic ticket management and child listing |
| `_health.py` | Health | Health check, uptime, Langfuse status, repos listing, board UI |
| `_passes.py` | Passes | Manual pass triggering and pass status |
| `_repo_helpers.py` | — (internal) | Shared helpers for board-id resolution |
| `_repos.py` | Repos | Per-repo configuration and status endpoints |
| `_system.py` | System | Drain (update-pending) mode controls |
| `_tickets.py` | Tickets | Ticket CRUD: create, list, read, update, transition |
| `_tickets_ingest.py` | Tickets | Ingest a batch of tickets from an external source |
| `_tickets_merge.py` | Tickets | Merge-related ticket operations |
| `_tickets_transitions.py` | Tickets | Ticket state transition endpoints |
| `_traces.py` | Traces | Langfuse trace lookup and recent traces listing |

## Health endpoint

`GET /health` returns the service status, uptime, and worker pool
health. The worker exposes a `/health/worker` variant with per-repo
concurrency details. When drain (update-pending) mode is armed, the
response also includes a `drain` object with the same shape as
`GET /system/drain`.

## Drain (update-pending) mode

`POST /system/drain` arms (or clears) drain mode: the worker stops
starting new heavy stages (implement / ci_fix / refine) while letting
in-flight ones finish, so the mill drains to a deployable quiet point.
The caretaker sets the flag when it sees a pending image update, polls
until `drained` is true, then deploys.

- `POST /system/drain` — arm drain mode. Body `{"ttl_seconds": 3600}`
  overrides the fail-open TTL (default 3600s); `{"enabled": false}`
  clears immediately.
- `GET /system/drain` — current status:
  `{"active", "drained", "drain_until", "active_stages"}`. `drained`
  is true when drain is armed and no heavy stage is in flight.

Fail-open: the flag is in-memory only (a restart clears it) and expires
after the TTL, so a stale flag whose caretaker has vanished can never
wedge intake.

## Board WebSocket

`WS /board/ws` streams real-time ticket state changes to connected
board clients. The `broadcaster.py` module manages client connections
and fans out state-change events.

## Ticket lifecycle

Tickets are created via `POST /tickets` with a `TicketCreate` body and
transition through states via `POST /tickets/{id}/transition`. The
transition endpoint validates the requested state against the state
machine and enqueues the ticket for worker processing.

## Langfuse status

`GET /health/langfuse-status` returns recent Langfuse export failures
from the in-memory ring buffer maintained by `tracing.py`. A
`POST /health/langfuse-status/clear` endpoint allows operators to
acknowledge and clear failure entries.

## Repos endpoint

`GET /repos` lists all configured repositories with their current
status, supporting per-repo and global views when the process is
running in multi-repo mode.
