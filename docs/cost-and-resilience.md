# Cost controls & resilience

> **Fleet-level cost monitoring** (dashboard, reconciliation, cost-analyst)
> has moved to **[robotsix-cost-monitor](https://github.com/robotsix/robotsix-cost-monitor)** —
> a standalone multi-Langfuse dashboard with OpenRouter↔Langfuse reconciliation
> and LLM cost analysis. This document covers only the remaining per-ticket
> cost cap backstop.

## Per-ticket cost (`cost_usd`)

Each ticket card on the board shows a cumulative LLM spend (e.g.
`$0.0943`), stored in the `ticket.cost_usd` DB column. Cost is derived
from **Langfuse session totals** — not from in-process accumulation.

### How it works

Every traced model call carries `session.id = <ticket id>` (set in
`runtime/tracing.py`). Langfuse attributes cost per session correctly
regardless of concurrency. Cost is read **on-demand** from the Langfuse
public API via `session_cost()` in `langfuse/client.py` — there is no
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
  which the board polls every 1 second — otherwise N cold-cache
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

### Cost baseline on redraft

When a ticket is redrafted (reset back to DRAFT from any active state),
the full Langfuse session cost at that moment is captured as
`pre_redraft_cost_usd`. The effective per-attempt cost is then computed
as `max(0.0, session_total - pre_redraft_cost_usd)`. This means:

- The dollar-cap limit restarts at zero for the new attempt — only spend
  accrued after the redraft counts toward the limit.
- The full session cost (including pre-redraft spend) remains available
  for informational/historical display via the `pre_redraft_cost_usd` baseline.
- A second redraft re-snapshots the baseline to the then-current session
  total, tracking the cost hierarchy across multiple attempts.

Worked example — a ticket redrafted twice:

- **Attempt 1** spends $5.00 → session total `$5.00`, baseline
  `pre_redraft_cost_usd = $0.00`, effective per-attempt cost
  `max(0.0, 5.00 - 0.00) = $5.00`.
- **Redraft** → baseline re-snapshotted to `pre_redraft_cost_usd = $5.00`;
  effective cost restarts at `max(0.0, 5.00 - 5.00) = $0.00`.
- **Attempt 2** spends $3.00 → session total `$8.00`, baseline
  `pre_redraft_cost_usd = $5.00`, effective per-attempt cost
  `max(0.0, 8.00 - 5.00) = $3.00`.

The informational/historical **total** (the raw Langfuse session total)
is `$8.00`, while the dollar cap only ever sees the `$3.00` effective
cost for the current attempt.

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

- **Dollar-cap safety net.** If a ticket's per-attempt Langfuse-traced
  LLM spend (effective cost after subtracting the pre-redraft baseline)
  exceeds `MILL_MAX_SPEND_USD_PER_TICKET` (default `0.0` = disabled),
  the worker escalates it to `BLOCKED`. The effective cost is computed
  as `max(0.0, session_total - pre_redraft_cost_usd)` so the limit
  restarts at zero when a ticket is redrafted. Enforced inline in
  `worker.py:_check_progress`.

- **No-progress safety net.** If a ticket re-enters the same
  model-driven stage `MILL_MAX_STUCK_CYCLES` times (default 3) without
  ever advancing — e.g. a run repeatedly killed before any checkpoint —
  the worker escalates it to `blocked` (resumable) and notifies, rather
  than silently re-billing the LLM on every requeue. Poll stages
  (`human_mr_approval` waiting on an open PR) are exempt.

- **Stage timeout.** If a single stage invocation runs longer than
  `MILL_STAGE_TIMEOUT_SECONDS` (default 1800 s = 30 min), the worker
  escalates the ticket to `BLOCKED` with a note and frees the worker
  slot.  Per-stage overrides are available via
  `MILL_STAGE_TIMEOUT_OVERRIDES` (JSON dict, e.g.
  `{"merge":0}` to disable timeout on merge).  This complements the
  stuck-cycle detector by catching hangs *within* a single stage
  invocation (hung LLM call, runaway shell command, asyncio deadlock).
  Set to 0 to disable entirely.

## See also

- [index.md](index.md) — documentation home
- [docs/configuration.md](configuration.md) — full env-var reference
- [docs/agents.md](agents.md) — agent catalog with model-var mapping
