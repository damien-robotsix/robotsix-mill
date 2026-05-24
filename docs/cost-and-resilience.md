# Cost controls & resilience

## Per-ticket cost (`cost_usd`)

Each ticket card on the board shows a cumulative LLM spend (e.g.
`$0.0943`), stored in the `ticket.cost_usd` DB column. Cost is derived
from **Langfuse session totals** — not from in-process accumulation.

### How it works

Every traced model call carries `session.id = <ticket id>` (set in
`runtime/tracing.py`). Langfuse attributes cost per session correctly
regardless of concurrency. Cost is read **on-demand** from the Langfuse
public API via `session_cost()` in `langfuse_client.py` — there is no
persistent store and no background sync loop. Results are cached for
**60 seconds** (`_COST_TTL_SECONDS`) to avoid hammering Langfuse on
board renders.

The actual population happens in `runtime/deps.py:with_cost`, which
mutates the ticket object in-place:

- **Blocking path** (`blocking=True`): calls `session_cost()` which
  hits Langfuse (or returns the cached value if within the TTL).
- **Non-blocking path** (`blocking=False`): calls `session_cost_cached()`
  which returns the cached value if present, else `0.0`, and **never**
  hits the network. This is used by the `/tickets` list endpoint,
  which the board polls every 5 seconds — otherwise N cold-cache
  tickets would issue N serial Langfuse HTTP calls.

The board and `/tickets` API read `cost_usd` directly from the DB
after in-place population — **zero Langfuse calls on render** once
cached. The ticket detail drawer likewise shows the cached value.

This design replaces an earlier periodic sync loop (`_cost_sync_loop`
+ `MILL_COST_SYNC_SECONDS`) that no longer exists. Cost lives in
Langfuse; mill reads and briefly caches it.

### Graceful degradation

When Langfuse is unconfigured (`LANGFUSE_*` env vars absent) or
unreachable, `session_cost()` returns `0.0` — no errors, no blocked
pipeline. The board displays `$0.0000`.

### Accuracy requirement

Accurate per-ticket cost **requires Langfuse configured** with all
three env vars (`LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`). Session-summed cost is only complete if every
trace carries the session id — the trace-health system enforces this
across all agent runs.

## Cost dashboard

The board's **Cost Dashboard** (💰 button in the drawer header) shows
aggregate spend across all tickets for a configurable lookback window
(1 hour – 7 days). It calls three Langfuse-backed endpoints in
parallel:

| Endpoint | What it returns |
|---|---|
| `GET /costs/by-agent?lookback_hours=N` | Per-agent-name cost bars (total cost + trace count) |
| `GET /costs/most-expensive-ticket?lookback_hours=N` | The single ticket with the highest LLM spend in the window |
| `GET /costs/most-expensive-trace?lookback_hours=N` | The single most expensive individual agent run (trace) in the window |

All three endpoints clamp `lookback_hours` to `[1, 168]` (same as the
selector options). When tracing is disabled or no data exists, the
most-expensive endpoints return `null` and the dashboard shows a muted
"No data" placeholder — the per-agent bar chart continues to render
independently.

### Langfuse functions

Two new functions in `langfuse_client.py` back the most-expensive
endpoints, following the same pagination and graceful-degradation
patterns as `aggregate_cost_by_name`:

- **`most_expensive_ticket(settings, lookback_hours)`** — groups traces
  by `sessionId`, sums `totalCost` per session, returns the session with
  the highest total cost (or `None` when tracing is disabled / the API
  errors). The route then looks up the matching ticket by `session_id`.

- **`most_expensive_trace(settings, lookback_hours)`** — scans traces
  for the single highest `totalCost`, skipping unnamed/in-flight traces
  (same `_named` filter as `list_recent_traces`). Returns the trace
  dict directly.

Both functions cap examination at 500 traces (`EXAMINE_CAP`) to bound
API calls, and catch all exceptions — returning `None` on failure
rather than crashing the dashboard.

## Cost controls

- **Implement agent + two lean sub-agents (each its own model).** A
  capable agent (`MILL_MODEL`) reads and edits the repo **itself**,
  kept lean by:
  - `explore(question)` — a cheap **scout** (`MILL_EXPLORE_MODEL`,
    `MILL_EXPLORE_REQUEST_LIMIT`) that returns concise pointers
    (paths/symbols/line-ranges), **never whole files**; the main
    agent then `read_file`s only what it needs.
  - `run_tests()` — a cheap **test sub-agent** (`MILL_TEST_MODEL`)
    runs the suite in the sandbox and **distills** failures into
    actionable feedback (never the raw log in the conversation).
  - `web_research(query)` — cheap web lookups, conclusion only, never
    `:online`.
  - `report_issue(title, body, category)` — **every** agent (built via
    `build_agent`) gets this by default: file a `source="agent"` DRAFT
    ticket when it hits a system issue (missing tool, error, workflow
    gap, missing input). Dedup-guarded — a looping agent can't spam the
    same ticket while a non-terminal one with that title exists.
  - `edit_file(path, old, new)` — preferred surgical-edit tool that
    replaces a unique substring; `write_file` is the fallback for
    new files or when `edit_file` can't apply.
  It loops read→edit→`run_tests` (≤`MILL_MAX_FIX_ITERATIONS`) until
  green or BLOCK-resumable. Refine likewise authors the spec with a
  `web_research` delegate. Each role has its own model so cheap models
  can be slotted in per-agent for cost leverage (all default to the
  capable model). No implement sub-agent and no `deep_*` layer — both
  re-explored everything and never converged.

- **Dollar-cap safety net.** If a ticket's cumulative Langfuse-traced
  LLM spend exceeds `MILL_MAX_SPEND_USD_PER_TICKET` (default `0.0` =
  disabled), the worker escalates it to `BLOCKED`. Enforced inline in
  `worker.py:_check_progress`.

- **No-progress safety net.** If a ticket re-enters the same
  model-driven stage `MILL_MAX_STUCK_CYCLES` times (default 3) without
  ever advancing — e.g. a run repeatedly killed before any checkpoint —
  the worker escalates it to `blocked` (resumable) and notifies, rather
  than silently re-billing the LLM on every requeue. Poll stages
  (`in_review` waiting on an open PR) are exempt.

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference
- [docs/agents.md](agents.md) — agent catalog with model-var mapping
