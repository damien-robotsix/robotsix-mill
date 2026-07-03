# ADR: Worker Process Split

> **Status:** draft  
> **Epic:** api-responsiveness  
> **Scope:** design only — no code changes in this ticket  

## Overview

The mill runs the FastAPI server (single uvicorn event loop), the worker
pool, ~15 periodic agent poll loops, and the
board UI websocket **in one Python process on one event loop**. Under
load the API becomes intermittently unresponsive. The epic proposes
seven children; this ADR evaluates whether the deepest fix — running
the worker + periodic engine in a separate process — is warranted, or
whether children 1-4 (non-blocking endpoints, offloaded periodic passes,
global concurrency cap, staggered startup) suffice.

## Current architecture

### Single process, single event loop

Uvicorn is started with **no `--workers` flag** (`cli/serve.py:24-60`).
The lifespan (`runtime/lifespan.py:100-128`) wires everything onto
`app.state` and starts the worker's consumer tasks and periodic loops as
`asyncio.create_task` coroutines on the same event loop.

### What already runs off the event loop

All potentially blocking work is offloaded to threads via
`asyncio.to_thread` (default `ThreadPoolExecutor`):

| Workload | Location | Mechanism |
|---|---|---|
| Stage execution (LLM agent runs) | `worker/processing.py:310` | `asyncio.to_thread(stage.run, ...)` |
| Periodic LLM passes (audit, survey, test-gap, etc.) | `worker/periodic_passes.py:197` | `_tracked_to_thread` → `asyncio.to_thread` |
| CI monitor checks | `worker/poll_loops.py:241,414,448` | `asyncio.to_thread` |
| DB maintenance | `worker/poll_loops.py:848` | `asyncio.to_thread` |
| Sandbox reaper | `worker/periodic_passes.py:537` | `asyncio.to_thread` |
| Trace health check | `worker/periodic_passes.py:591` | `asyncio.to_thread` |

The event loop itself is never directly blocked by stage work, periodic
passes, or git operations — they all run in threads.

### GIL release in blocking paths

The GIL is not the primary bottleneck here because every heavy path
releases it:

- **SQLite** — CPython's `sqlite3` module releases the GIL during C-level
  query execution.
- **Subprocess calls** (`git`, `docker`) — `subprocess.run` /
  `subprocess.Popen` release the GIL while waiting on the child process.
- **HTTP calls** (`httpx`, Langfuse API) — I/O operations release the GIL.
- **Filesystem I/O** (`RunRegistry` JSON reads/writes) — kernel I/O
  releases the GIL.

The CPU-bound work that *holds* the GIL (prompt construction, response
parsing, JSON serialization, DB result processing) is brief relative to
the I/O-dominated work.

### What still blocks the event loop

Two things cause the observed latency:

1. **Synchronous enrichment in GET `/tickets` and `/board/cards`.**
   `runtime/routes/_tickets.py:233` runs `enrich_ticket_read` in a
   synchronous list comprehension inside the route handler. Even though
   `blocking_cost=False` avoids Langfuse HTTP calls, it still runs
   per-ticket SQLite queries synchronously on the event loop thread.

2. **Scheduling pressure.** ~15 periodic async tasks + per-repo consumer
   tasks + poll loops all compete for the same event loop. Post-restart,
   all periodic passes fire their first tick simultaneously (thundering
   herd), amplifying the pressure.

Children 1 and 4 directly target these: child 1 offloads `/tickets`
enrichment to a thread, child 4 staggers startup.

## What a process split would require

### Shared state that must cross the boundary

| State | Current form | Split impact |
|---|---|---|
| `TicketService` | Single in-process instance; called by worker to transition tickets, add comments, query DB. | Worker process would access the same SQLite files directly (WAL mode supports concurrent readers, but writes must be serialized). The per-board consumer-task serialization already exists. |
| `BoardBroadcaster` | In-process `asyncio.Queue` per WebSocket client; `broadcast_sync` uses `call_soon_threadsafe`. | Needs a cross-process notification channel. Options: Redis pub/sub, a Unix socket, or a dedicated HTTP endpoint on the API process that the worker calls on ticket transitions. |
| `service._on_transition` callback | Wired in `lifespan.py:116`: `service._on_transition = broadcaster.broadcast_sync`. | The worker's `TicketService` would need to POST to the API process's internal broadcast endpoint on every state change. |
| `RunRegistry` | Thread-safe, file-backed JSON at `<data_dir>/<board_id>/runs.json`. | Already file-backed; both processes can read/write the same files with per-file `threading.Lock` (or an `fcntl` lock in a multiprocess world). Minor adaptation needed. |
| `Settings` / `ReposRegistry` | Configuration loaded at startup. | Both processes would independently load the same config. Trivial. |

### IPC design options

| Option | Complexity | Latency | Operational weight |
|---|---|---|---|
| **Redis pub/sub** | Medium — new dependency, new connection management | <1ms on loopback | New always-on service in docker-compose |
| **Internal HTTP endpoint** | Low — worker POSTs to API on transition | ~1ms on loopback | No new dependencies; API already serves HTTP |
| **Unix datagram socket** | Medium-High — custom framing, reconnection logic | <0.1ms | No new dependencies; OS-level |
| **Shared SQLite + polling** | Low — worker writes to a `broadcast_queue` table, API polls it | Poll interval latency (100ms+) | No new dependencies; simplest but laggy |

The internal HTTP endpoint is the pragmatic choice: the worker calls
`POST http://localhost:{api_port}/_internal/broadcast` with the ticket
JSON. The API already serves HTTP; the endpoint is unauthenticated
localhost-only (matching the existing management-API posture).

### Deployment complexity

- **One container, two processes:** The entrypoint would start both
  `robotsix-mill serve` (API) and `robotsix-mill worker` (new command)
  via a process supervisor (e.g. `s6-overlay`, `supervisord`, or a
  simple shell script with `wait`). Health checks would need to cover
  both.
- **Two containers:** The worker and API run in separate Compose
  services, sharing the data volume. Cleaner isolation but more YAML
  and two images (or one image with two commands).

Either approach doubles the number of things that can fail and must be
monitored.

## Recommendation

**Do not split now. Implement children 1-4 and re-evaluate.**

### Rationale

1. **Children 1-4 address the known blocking paths directly.**
   - Child 1: offload `/tickets` and `/board/cards` enrichment to a
     thread so the route handler returns immediately.
   - Child 2: periodic passes are already offloaded; the remaining
     work is verifying no synchronous path was missed.
   - Child 3: a global concurrency cap prevents the thread pool from
     being exhausted by too many simultaneous agent runs.
   - Child 4: staggered startup (jittered initial delays per periodic
     pass, rate-limited requeue) eliminates the post-restart thundering
     herd.

2. **The GIL is not the bottleneck.** Every heavy path (SQLite, git,
   HTTP, subprocess) releases the GIL. The event-loop blocking comes
   from synchronous enrichment in route handlers and scheduling
   pressure, not from CPU-bound work holding the GIL.

3. **The process split adds real complexity.** IPC for
   `BoardBroadcaster`, coordination of `TicketService` DB access across
   processes, deployment topology changes, health-check coverage for two
   processes — all for a problem whose root causes have simpler fixes.

4. **The split remains a valid escape hatch.** If children 1-4 land and
   latency issues persist, the split design documented above provides a
   clear path: add an internal `POST /_internal/broadcast` endpoint,
   extract the worker into a `robotsix-mill worker` subcommand, and run
   both under a supervisor. The decision is reversible — nothing in
   children 1-4 makes a future split harder.

### Conditions that would trigger re-evaluation

- `/health` latency (p99) remains above 1s after children 1-4 land.
- Profiling shows CPU-bound work (not I/O) consuming >100ms per tick on
  the event loop thread.
- The thread pool (`default_executor`) is not saturated but the API
  still sees intermittent 5s+ response times.

## Future split design (reference)

If the split is pursued later, the minimal design is:

1. **New CLI subcommand:** `robotsix-mill worker` that constructs
   `TicketService`, `StageContext`, `Worker` and calls `worker.start()`
   without starting an HTTP server.

2. **Internal broadcast endpoint:** `POST /_internal/broadcast` on the
   API process (localhost-only, no auth). The worker's
   `_on_transition` callback POSTs ticket JSON to this endpoint instead
   of calling `broadcaster.broadcast_sync` directly.

3. **Shared SQLite:** Both processes open the same per-repo `.db` files
   with `check_same_thread=False` and WAL journal mode. The worker's
   existing per-board consumer-task serialization prevents concurrent
   writes to the same board's DB.

4. **Process supervisor:** The entrypoint starts both processes with a
   lightweight supervisor. If either exits, the container exits (matching
   the current single-process failure mode).

## Acceptance criteria

- [x] Document committed at `docs/design/worker-process-split.md`.
- [x] Investigates shared in-process state: `TicketService`,
      `BoardBroadcaster`, `RunRegistry`, `app.state` wiring.
- [x] Describes IPC / shared-DB model for the split scenario.
- [x] Explains how board broadcasts would cross the process boundary.
- [x] Clear recommendation: do not split now; implement children 1-4
      first and re-evaluate with latency data.
