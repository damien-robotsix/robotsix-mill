# Debugging & Observability Runbook

This is the operator's single starting point for diagnosing and
recovering from a failed or misbehaving ticket run. It consolidates the
troubleshooting knowledge that is otherwise scattered across the
architecture docs, and connects the observability signals — the board
UI, Langfuse traces, request-ID-correlated logs, the run registry,
Prometheus metrics, and preserved workspaces — to a concrete diagnosis
workflow.

It does **not** duplicate the authoritative docs. Where a failure has a
dedicated page (cycle detection, OOM tuning, configuration), this
runbook tells you *when* you are in that situation and links you to the
single source of truth. See [Cross-references](#cross-references).

> **Scope.** This guide is about diagnosing runs, not about the
> pipeline's normal operation. For the stage lifecycle see
> [runtime/worker.md](../runtime/worker.md); for the agent catalog see
> [agents/index.md](../agents/index.md).

---

## 1. Observability stack overview

robotsix-mill exposes five complementary observability surfaces. Each
answers a different question; effective debugging means moving between
them, not living in one.

| Surface | Answers | Where |
|---|---|---|
| **Board UI** | *What state is this ticket in? Which stage failed?* | `GET /board` |
| **Langfuse traces** | *What did the agent actually do this run — prompts, tool calls, tokens?* | Langfuse web UI, per ticket |
| **Logs (request-ID correlated)** | *What happened in the process at that moment, across concurrent tickets?* | container logs |
| **Run registry** | *When did each periodic pass last run, and did it succeed?* | `GET /health` |
| **Prometheus metrics** | *Is the service healthy in aggregate — latency, request volume, cost?* | `GET /metrics`, `GET /metrics/step-usage` |

### Request-ID correlation across logs

Every HTTP request is tagged with a request id by
`RequestIDMiddleware` in
`src/robotsix_mill/runtime/middleware.py`. The middleware reads an
incoming `X-Request-ID` header if the caller supplied one, otherwise it
generates a fresh `uuid.uuid4().hex`. The id is:

- stored in a `ContextVar` (and `scope["state"]`) so route handlers and
  any code on the same async task can read it,
- echoed back on the response as the `X-Request-ID` header, and
- injected into every log record by `RequestIDLogFilter`, which the log
  formatter renders as `[%(request_id)s]` (the formatter is patched in
  `runtime/lifespan.py`).

**Why it matters:** the worker processes up to `MILL_MAX_CONCURRENCY`
tickets in parallel, so their log lines interleave. Grepping for a
single request id lets you reconstruct one request's timeline out of
the interleaved stream:

```sh
docker compose logs mill | grep '\[<request-id>\]'
```

You get the request id from the `X-Request-ID` response header of the
API call you made (e.g. a manual transition or pass trigger), or from
the first log line the operation emitted.

### How Langfuse traces map to ticket runs

Tracing is implemented in `runtime/tracing.py` (delegating the OTLP →
Langfuse plumbing to `robotsix_llmio.core.tracing`). The mapping is:

- **One root span per stage run.** `start_ticket_root_span(ticket_id,
  stage_name)` opens a root OTel span named after the stage (`refine`,
  `implement`, `review`, …). Sub-operations become child spans via
  `trace_stage(...)`.
- **The Langfuse session id is the ticket id**, repo-qualified as
  `<repo> · <ticket-id>` so a shared project's session list stays
  legible. To find a ticket's runs in Langfuse, search the session list
  for the ticket id.
- **Per-turn cost is recorded on the span** by `record_step_usage(...)`
  as `langfuse.observation.metadata.mill.step_usage` — token counts and
  tool-call counts per turn, for cost analysis.
- **Input/output payloads** are attached to the root span (JSON,
  capped at 8,000 chars) and rendered at the trace level.

Langfuse is **global**: one project configured in the `secrets:` block
of `config/config.json`, applied uniformly to every repo. There is no
per-repo Langfuse project. See
[langfuse/observability.md](../langfuse/observability.md) for the
configuration surface.

If traces are missing, check `GET /health/langfuse-status` — it returns
a ring buffer of recent export failures maintained by `tracing.py`. No
credentials configured means the whole tracing layer is a silent no-op
(by design), so absent traces can simply mean Langfuse was never wired
up.

### Prometheus metrics

Metrics are exposed at **`GET /metrics`**, wired via
`prometheus_fastapi_instrumentator` in
`src/robotsix_mill/runtime/api.py`. If the instrumentator package is
not installed, a warning is logged and `/metrics` is simply
unavailable — it is not fatal. The default instrumentation covers
request counts, latency histograms, and request/response sizes.

A second endpoint, **`GET /metrics/step-usage`**
(`runtime/routes/_step_usage.py`), returns server-side stage × model
token aggregates computed from the local SQLite mirror — useful for
cost attribution without round-tripping to Langfuse.

### Board UI and the run registry

The Kanban board at `GET /board` shows every ticket across the
automated pipeline columns; the column *is* the stage, so the column a
ticket is stuck in tells you which stage to investigate. See
[runtime/board.md](../runtime/board.md).

The **run registry** (`runtime/run_registry.py`) is the durable record
of *periodic* pass executions (audit, health, trace-health, …), surfaced
via `GET /health`. Each `RunEntry` records `kind`, `started_at`,
`finished_at`, `status` (`running` / `ok` / `error`), a `summary`, and
an `error` string. Two properties matter when debugging:

- Only `ok` entries reset a pass's due-timer, so an errored or
  interrupted run does not silently push the next fire window out.
- Entries left `running` after an unclean restart are reconciled to
  `error` with `"interrupted by process restart"` on load — so a stale
  `running` entry in the UI is itself a signal that the process died
  mid-pass (see [OOM pressure](#oom-memory-pressure)).

---

## 2. Step-by-step debugging workflow

Follow the signals from coarse to fine. Most investigations resolve in
the first two steps.

```
        ┌─────────────────────────────────────────────┐
        │ START: "a ticket run failed / is stuck"      │
        └───────────────────────┬─────────────────────┘
                                 │
                                 ▼
  1. BOARD UI  ── which column (stage) is the ticket in?
        │
        ├─ BLOCKED ──────────────► go to §3 (match the failure mode)
        ├─ ERRORED ──────────────► worker-level crash → check logs + heartbeat (§3 OOM)
        ├─ stuck "retrying" ─────► §3 "Stuck retries"
        ├─ stuck at human gate ──► approval pending, not a failure
        └─ moving normally ──────► not stuck; observe
                                 │
                                 ▼
  2. LANGFUSE (per ticket) ── open the session = <repo> · <ticket-id>
        │   inspect the failed stage's root span:
        │   - last tool call before failure?
        │   - error text in the output payload?
        │   - token blow-up / truncation?
        └─ still unclear ────────►
                                 │
                                 ▼
  3. LOGS (request-ID correlated) ── grep the container logs
        │   - find the stage's request id, grep '[<id>]'
        │   - look for tracebacks, retry/backoff lines, SIGKILL gaps
        └─ points at workspace state ►
                                 │
                                 ▼
  4. WORKSPACE INSPECTION ── if preserved (§4), inspect the clone,
        git state, dependency tree, and artifacts/ error logs.
```

### Step 1 — Board UI: identify the stage and state

Open the board (or `robotsix-mill ticket show <id>`). The column names
the stage; the state tells you the failure class:

- **BLOCKED** — a fatal stage error, exhausted retries, or a cycle. The
  `[cycle-detected]` comment (if present) means a dependency cycle — go
  to [cycles.md](../cycles.md), not to logs.
- **ERRORED** — a rare worker-level crash. Usually a process-level
  event (OOM, restart), not a stage bug.
- **retrying** — a transient error is being retried with backoff.

Never hand-edit the database to move a ticket. Use the CLI, which
respects the state machine:

```sh
robotsix-mill ticket state <id> <new-state>
```

### Step 2 — Langfuse: replay what the agent did

Open the Langfuse session for the ticket (`<repo> · <ticket-id>`) and
select the failed stage's root span. The span's input/output payloads
and `step_usage` metadata reconstruct the run: the last successful tool
call, the error surfaced to the agent, and whether the context blew up
(a large-context implement pass is also an OOM risk — see §3).

### Step 3 — Logs: reconstruct the process timeline

When Langfuse is inconclusive (or tracing is disabled), grep the
container logs by request id (see
[request-ID correlation](#request-id-correlation-across-logs)). Look
for:

- Python tracebacks (the proximate exception),
- `stage_retry` backoff lines (transient-error churn),
- an abrupt end with **no** shutdown lines followed by a Docker restart
  (a SIGKILL — suspect OOM, §3).

### Step 4 — Workspace inspection

If the workspace clone was preserved (§4), inspect the on-disk state
the agent left behind: final git status, dependency tree, and the
`artifacts/` error logs.

---

## 3. Common failure modes

For each mode: **symptoms → diagnosis → recovery**. Where a dedicated
doc owns the topic, this section only helps you recognize the mode and
routes you there.

### Sandbox execution errors

**Symptoms.** A stage (implement / refine) fails with a command that
never ran, exited non-zero unexpectedly, or reports a missing tool or a
path outside the sandbox. The worker itself is healthy; only the
stage's delegated command failed.

**Diagnosis.** The worker never runs agent commands directly — the
implement and refine stages delegate to the **sandbox** subsystem,
which spawns a fresh disposable Docker container per command. Check the
stage's Langfuse span for the exact command and its captured
stdout/stderr, then the logs for container-spawn errors. Confirm the
command respects path confinement — the sandbox is network-isolated and
path-confined by design.

**Recovery.** Fix the underlying command/spec issue, then re-run only
the failed stage:

```sh
robotsix-mill ticket state <id> resume-blocked
```

See [sandbox/security.md](../sandbox/security.md) for the security model
and [docker-architecture.md](../docker-architecture.md) for the
container topology.

### OOM / memory pressure

**Symptoms.** The process dies abruptly with **no** Python traceback and
**no** uvicorn shutdown lines, followed by a Docker auto-restart that
kills in-flight sandbox spawns. Tickets that were mid-stage land in
ERRORED, and run-registry entries that were `running` show up as
`interrupted by process restart`.

**Diagnosis — don't guess.** The mill persists a crash-diagnostic
heartbeat (`heartbeat.json` in the data dir). On the next startup, when
the previous run never reached graceful shutdown, it logs:

```
previous process ... died abruptly ... suspected OOM kill
```

and reads the cgroup `oom_kill` counter where available. Large-context
implement work is the usual trigger — prompts of ~139k tokens were
observed in flight at crash time. The mill worker is single-process, so
the `mem_limit: 4g` ceiling in `docker-compose.yml` must cover the LLM
streaming buffers *plus* the worker pool and subprocesses.

**Recovery.** Raise `mem_limit` (e.g. `6g`–`8g`) and/or add a
`mem_reservation` in `docker-compose.override.yml`, then resume the
affected tickets with `resume-blocked`. Full tuning guidance and the
incident write-up live in
[dev-tooling/deployment.md](../dev-tooling/deployment.md#oom-pressure-under-large-context-implement-work).

### Config loading errors

**Symptoms.** The process fails to start, a repo is silently skipped, or
a newly added setting appears to have no effect.

**Diagnosis.** A setting that has no effect is usually **config drift** —
a Pydantic field that was never wired to `config/config.example.json`
and the model alias in the same commit is invisible to the sync
checker. Confirm the field exists on both surfaces. For a
`config/repos.yaml` value that seems ignored, remember there is **no**
`${ENV_VAR}` interpolation in that file (use literal paths), and a
per-repo `langfuse:` block is silently ignored (Langfuse is global).

**Recovery.** Fix the config surface, restart the worker, and re-run
`uv run python scripts/emit_config_schema.py --check` if you changed a
settings field. The full env-var reference and the drift-prevention rule
are in [config/configuration.md](../config/configuration.md).

### Stuck retries

**Symptoms.** A ticket sits in a `retrying` state, cycling through
backoff without progressing, or repeatedly re-enters the same stage.

**Diagnosis.** Transient stage failures (git outage, provider 5xx) are
retried with exponential backoff by `runtime/stage_retry.py`
(configurable via `MILL_STAGE_RETRY_*`). A ticket that never clears is
hitting a non-transient error being misclassified as transient, or the
external dependency is genuinely down. Grep the logs by request id for
the repeated backoff lines and the underlying exception. For the
implement stage specifically, the fix loop is bounded
(`max_fix_iterations`, default 8) and escalates to BLOCKED on repeated
no-progress passes — a ticket that reached BLOCKED this way has already
exhausted its automatic retries.

**Recovery.** Once the root cause is addressed, clear the retry state
and re-enqueue:

```sh
robotsix-mill ticket state <id> resume-blocked
```

Exhausted retries land in BLOCKED; from there `resume-blocked` re-runs
only the failed stage, while a manual override to `READY`/`DRAFT`
forces a full re-run. No raw database editing is ever needed — see
[runtime/worker.md](../runtime/worker.md#blocked-recovery).

### Merge-poll failures

**Symptoms.** A ticket sits at `human_mr_approval` (PR open) long after
the PR was merged, or never advances to `done`.

**Diagnosis.** `human_mr_approval` means the PR is the review; the
merge-poll loop (`PollLoopsMixin` in the worker) flips the state once it
observes the merge. If it never flips, the poll loop is not seeing the
merge — check forge credentials/connectivity and the poll-loop log
lines for the ticket's PR. CI status is polled on the same tick-based
loop.

**Recovery.** Restore forge connectivity; the next poll tick advances
the ticket. If the PR truly merged but the poll missed it, a manual
state transition can move the ticket forward — verify the merge on the
forge first.

### Dependency cycles (deadlocked BLOCKED tickets)

**Symptoms.** Several BLOCKED tickets never auto-resume and each carries
a `[cycle-detected]` comment listing a cycle path (e.g. `A → B → C →
A`).

**Diagnosis & recovery.** This is a circular `depends_on` / `unblocks`
graph that auto-resume cannot break. Do not go to the logs — go
straight to [cycles.md](../cycles.md), which owns detection and
resolution (sever one edge, or close a member).

---

## 4. Workspace inspection (post-mortem)

By default, a ticket's `repo/` clone is deleted when it closes
(`MILL_PRUNE_CLONE_ON_CLOSE=true`), and a backstop GC prunes clones in
terminal-ticket workspaces after a day. That reclaims disk but destroys
the on-disk evidence you need for a post-mortem.

**To preserve the clone for inspection**, set:

```sh
MILL_PRUNE_CLONE_ON_CLOSE=false
```

With pruning disabled, the `repo/` clone survives after the ticket
finishes, so you can inspect the exact state the agent left:

```sh
cd <data-dir>/<board>/<ticket-id>/repo

git status                 # uncommitted/failed edits
git log --oneline -10      # what the agent committed
git diff HEAD~1            # the last change
uv tree                    # resolved dependency tree (dependency errors)
```

Note that `description.md` and the entire `artifacts/` tree
(`implement.md`, `retrospect.md`, stage logs, `screenshots/`) are
**always** preserved regardless of the prune setting — start there for
the agent's own account of the run, then drop into the clone for ground
truth. Pruning is best-effort: a failed delete is logged, never raised.

Full pruning/GC semantics are in
[core/workspace-cleanup.md](../core/workspace-cleanup.md).

---

## 5. Health & drain endpoints (quick reference)

| Endpoint | Use |
|---|---|
| `GET /health` | Service status, uptime, worker health, run-registry results |
| `GET /health/worker` | Per-repo concurrency detail |
| `GET /health/langfuse-status` | Recent Langfuse export failures (ring buffer) |
| `POST /health/langfuse-status/clear` | Acknowledge/clear export-failure entries |
| `GET /metrics` | Prometheus request/latency/size metrics |
| `GET /metrics/step-usage` | Stage × model token aggregates (cost) |
| `GET /system/drain` / `POST /system/drain` | Drain mode — stop starting heavy stages so the mill quiesces before a deploy |

See [runtime/routes.md](../runtime/routes.md) for the full route
inventory.

---

## Cross-references

The docs below remain the **single source of truth** for their topics;
this runbook only points you to the right one.

- [cycles.md](../cycles.md) — dependency-cycle detection and resolution.
- [dev-tooling/deployment.md](../dev-tooling/deployment.md) — OOM
  diagnostics, `mem_limit` tuning, and the heartbeat/`oom_kill` clues.
- [config/configuration.md](../config/configuration.md) — full env-var
  reference and config-drift prevention.
- [core/workspace-cleanup.md](../core/workspace-cleanup.md) — clone
  pruning and data-dir GC semantics.
- [runtime/tracing.md](../runtime/tracing.md) — Langfuse tracing
  architecture.
- [runtime/run-registry.md](../runtime/run-registry.md) — periodic-pass
  run registry.
- [runtime/worker.md](../runtime/worker.md) — stage lifecycle, retries,
  and BLOCKED recovery.
- [runtime/board.md](../runtime/board.md) — the Kanban board and column
  automation.
- [runtime/routes.md](../runtime/routes.md) — API and health endpoints.
- [langfuse/observability.md](../langfuse/observability.md) — Langfuse
  and deployed-log configuration.
- [sandbox/security.md](../sandbox/security.md) — sandbox execution
  model.
- [index.md](../index.md) — documentation home.
