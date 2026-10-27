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
- **Merge-poll lane:** tickets in `implement_complete`,
  `human_mr_approval`, and `waiting_auto_merge` bypass the global
  concurrency gate (`MILL_MAX_GLOBAL_CONCURRENCY`). Their work is
  forge-API bound and never starts an agent, so a green PR is promoted
  and auto-merged within one poll interval even while implement and
  ci_fix stages hold every non-classify slot. If such a run moves the
  ticket to an agent stage (e.g. `fixing_ci`, `rebasing`), that stage
  waits for a normal gate slot.
- Each worker tick logs `merge poll starved: N tickets at
  implement_complete queued while slots full` when the gate is saturated
  and implement_complete tickets are queued behind their board's own
  consumer pool.

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

## CI monitor: Queue-timeout re-dispatch

The CI monitor (`_poll_one_repo_ci()`) detects when a workflow run
concludes `cancelled` because the self-hosted runner was offline and
GitHub auto-cancelled the queued job after 24 hours. Rather than filing
a ticket (which would mislead the operator into thinking the code is
broken), the monitor **automatically re-dispatches the workflow** via
`workflow_dispatch` once the runner comes back online.

### Detection

A cancelled run is treated as a queue timeout when:
1. Its `conclusion` is `"cancelled"`
2. It never **started** a job (empty `jobs` list)

A concurrency-cancelled run (one that started jobs but was cancelled by
GitHub's concurrency control) is routine churn and is ignored.

### Re-dispatch strategy

- Once a runner is back online (`has_online_runner()` returns true),
  the monitor calls `dispatch_workflow()` to re-trigger the workflow
  on the current target branch.
- Re-dispatch is bounded: max **3 attempts** per cancelled run, spaced
  **6 hours apart** (via `_REDISPATCH_MIN_INTERVAL_SECONDS`). This
  prevents spam if the runner stays offline or if the workflow lacks a
  `workflow_dispatch` trigger.
- Attempt count is persisted per cancelled run (keyed by workflow id +
  head SHA) in the CI monitor state file.
- A **ticket is filed only if a re-dispatched run then fails** (handled
  by the ordinary CI failure path). If the re-dispatch succeeds, no
  ticket is created.

### Example workflow

1. **2026-10-07 10:00 UTC**: Publish Image workflow queued on main, runner offline
2. **2026-10-08 10:00 UTC**: GitHub auto-cancels due to 24h queue timeout
3. **2026-10-08 10:30 UTC**: Runner comes back online
4. **2026-10-08 10:31 UTC**: CI monitor detects cancelled run + online runner,
   re-dispatches via `workflow_dispatch`
5. **2026-10-08 10:35 UTC**: Publish Image workflow re-runs, succeeds
6. **Result**: Image artifact for the current head is now up-to-date; no ticket filed

### Scope

This re-dispatch behavior applies **only to repos and workflows that**:
- Use self-hosted runners (GitHub-hosted-only repos return `False` from
  `has_online_runner()`).
- Have the `workflow_dispatch` trigger configured (workflows without it
  fail silently and are throttled by the 6-hour interval so dispatch
  spam is prevented).

## CI auto-close periodic pass

The ci-auto-close pass runs every 15 minutes and closes `source=ci`
tickets when their workflow is green on the merge target (main).

### Cron-only workflow dispatch

When a workflow has **only** `schedule` + `workflow_dispatch` triggers
(no `push` / `pull_request`), it never re-runs on its own after a fix
PR merges. The stale red run lingers until the next scheduled tick,
keeping the ci ticket unresolved for up to a week.

**Auto-dispatch behavior**: When ci-auto-close detects a cron-only
workflow still red on the stale failing commit, it automatically triggers
one `workflow_dispatch` run on the merge target (`main`). This ensures
a fresh run completes within minutes, allowing the next pass to close
the ticket (or re-block it with the new run's result).

**Guards**:
- Dispatch at most once per ticket (checked via ticket history).
- Skip when a run newer than the failing commit already exists (a fresh
  run is on its way).

The dispatch is recorded in the ticket's event history so it's visible
in the UI and prevents accidental double-dispatch across restarts.

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
