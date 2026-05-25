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

When Langfuse is unconfigured (Langfuse secrets absent from
`config/secrets.yaml`) or unreachable, `session_cost()` returns `0.0`
— no errors, no blocked pipeline. The board displays `$0.0000`.

### Accuracy requirement

Accurate per-ticket cost **requires Langfuse configured** with
`langfuse_public_key`, `langfuse_secret_key`, and `langfuse_base_url`
set in `config/repos.yaml` for the selected repo. Session-summed cost is only complete if
every trace carries the session id — the trace-health system enforces
this across all agent runs.

## Cost dashboard

The board's **Cost Dashboard** (💰 button in the drawer header) shows
aggregate spend across all tickets for a configurable lookback window
(1 hour – 7 days). It calls three Langfuse-backed endpoints in
parallel:

| Endpoint | What it returns |
|---|---|
| `GET /costs/trend?lookback_hours=N&repo_id=X` | Time-bucketed cost for the sparkline chart |
| `GET /costs/by-agent?lookback_hours=N&repo_id=X` | Per-agent-name cost bars (total cost + trace count) |
| `GET /costs/most-expensive-ticket?lookback_hours=N&repo_id=X` | The single ticket with the highest LLM spend in the window |
| `GET /costs/most-expensive-trace?lookback_hours=N&repo_id=X` | The single most expensive individual agent run (trace) in the window |

All four endpoints accept **two mutually exclusive filter modes**:

| Parameter | Mode | Clamping | Description |
|---|---|---|---|
| `lookback_hours` (default `24`) | Time-window | `[1, 168]` | All traces in the last *N* hours |
| `max_tickets` (optional) | Last-N-tickets | `[1, 1000]` | All traces belonging to the last *N* distinct ticket sessions |

When both parameters are present, `max_tickets` takes precedence and
the time window is ignored (with a debug-level log noting the override).
The frontend never sends both — a **mode toggle** in the cost dashboard
switches between time-window (with options 1h/6h/24h/3d/7d) and
ticket-count mode (with options 20/100/1000).  On first load the
dashboard defaults to time-window, 24 hours.

When tracing is disabled or no data exists, the most-expensive
endpoints return `null` and the dashboard shows a muted "No data"
placeholder that adapts its wording to the active mode — the per-agent
bar chart continues to render independently.

The optional `repo_id` query parameter scopes the query to a single
repo's Langfuse project.  Use `repo_id=all` to aggregate across every
registered repo.  When omitted in single-repo mode the sole repo is
used; in multi-repo mode the parameter is required.

### Langfuse functions

Four aggregation functions in `langfuse_client.py` back the cost
endpoints, each accepting both `lookback_hours` and an optional
`max_tickets`:

- **`aggregate_cost_trend(settings, lookback_hours=24, max_tickets=None)`** —
  returns time-bucketed cost.  In time-window mode buckets span the
  lookback period (hourly if ≤ 24 h, daily otherwise).  In ticket-count
  mode buckets span the time range covered by the collected traces,
  with the same hourly/daily rule applied to that span.

- **`aggregate_cost_by_name(settings, lookback_hours=24, max_tickets=None)`** —
  aggregates `totalCost` and trace count by agent/stage name.

- **`most_expensive_ticket(settings, lookback_hours=24, max_tickets=None)`** —
  groups traces by `sessionId`, sums `totalCost` per session, returns
  the session with the highest total cost (or `None` when tracing is
  disabled / the API errors).  The route then looks up the matching
  ticket by `session_id`.

- **`most_expensive_trace(settings, lookback_hours=24, max_tickets=None)`** —
  scans traces for the single highest `totalCost`, skipping
  unnamed/in-flight traces (same `_named` filter as
  `list_recent_traces`).  Returns the trace dict directly.

All four functions share a common helper, `_fetch_traces_for_tickets`,
which paginates Langfuse traces by `timestamp.desc` (no `fromTimestamp`),
tracks distinct `sessionId` values, and stops after collecting traces
from the requested number of distinct sessions.

**Safety caps:** In time-window mode `most_expensive_ticket` and
`most_expensive_trace` cap at 500 traces (`EXAMINE_CAP`).  In
ticket-count mode all four functions cap at 100 pages × 100 traces
(10 000 traces).  All functions catch exceptions and return gracefully
(`None` or `[]`) rather than crashing the dashboard.

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
