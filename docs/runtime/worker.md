# Worker

The runtime worker is an in-process, event-driven pool that picks up
tickets and chains them through the pipeline stages.

## Architecture

The worker lives inside the same FastAPI process as the HTTP API — it
shares the SQLite database connection, the `ReposRegistry`, and the
tracing provider. It is started and stopped by the FastAPI lifespan
handler (`runtime/lifespan.py`), which also manages the background
periodic pass scheduler and the poll loops.

### Core loop (`worker/core.py`)

The `Worker` class assembles three mixins:

- **`PeriodicPassesMixin`** — schedules and runs periodic background
  passes (audit, health, agent-check, trace-health, etc.) on
  configurable intervals.
- **`PollLoopsMixin`** — polls for merge completion, CI status, and
  other async external events on a tick-based loop.
- **`Worker` (core)** — owns the main ticket-processing loop and the
  per-repo run registries.

### Ticket processing (`worker/processing.py`)

When a ticket is emitted or transitions state, the API enqueues it. The
worker's main loop picks it up immediately and chains stages:

```
emit ticket → API inserts row + enqueues → worker chains stages
  draft → refine → human_issue_approval → ready → implement → deliverable
  → human_mr_approval → (PR merged; merge-poll) → done → retrospect → closed
```

- **`human_issue_approval`** is a human gate (configurable via
  `MILL_REQUIRE_APPROVAL`).
- **`human_mr_approval`** = PR open (the PR is the review); the merge
  poll loop flips it.
- **`retrospect`** audits the run + Langfuse and may spawn an
  improvement draft.
- **`closed`** = terminal. **`errored`** = worker-level crash (rare).
  **`blocked`** = needs human intervention.

### Concurrency

- **`MILL_MAX_CONCURRENCY`** (default 4) controls how many tickets the
  worker processes in parallel. Distinct tickets run concurrently;
  stages within a single ticket are always serial.
- A **deduplication set** prevents the same ticket from being processed
  twice simultaneously.

### Epic handling (`worker/epic.py`)

Epic tickets spawn children that are processed independently. When all
children reach a terminal state, the parent epic is re-evaluated.

## Transient errors and retry

Transient stage failures (git outage, provider 5xx) are retried
automatically with exponential backoff via `runtime/stage_retry.py`
(configurable through `MILL_STAGE_RETRY_*` environment variables).

Fatal stage errors and exhausted retries go straight to **BLOCKED**.

### BLOCKED recovery

```
BLOCKED → resume-blocked → <blocked_from>   (re-run only the failed stage)
BLOCKED → READY | DRAFT                      (manual override: full re-run)
retrying ticket → resume-blocked             (clears retry state, re-enqueues)
```

No raw database editing is ever needed for recovery.

## Run registry

The worker maintains a `RunRegistry` (`runtime/run_registry.py`) —
an in-memory + file-backed record of periodic pass results. See
[run-registry.md](run-registry.md) for details.

## Tracing

The worker integrates with Langfuse for observability. Each stage run
opens a root OTel span named after the stage (e.g. `"refine"`,
`"implement"`) with the ticket id as the Langfuse session id. See
[tracing.md](tracing.md) for the full tracing architecture.

## Relationship to the sandbox

The worker itself never executes agent commands directly. The implement
and refine stages delegate command execution to the **sandbox**
subsystem, which creates a fresh, disposable Docker container per
command. See [docs/sandbox/security.md](../sandbox/security.md) for the
security model and [docs/docker-architecture.md](../docker-architecture.md)
for the container topology.
