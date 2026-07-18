# Trace-health check

The trace-health check is a **deterministic, no-LLM** check that scans
Langfuse for traces in the last 24 hours that are missing a `sessionId`
(unsessioned). Sub-agent / coordinator traces can fail to inherit the
ticket root span's `session.id`, and those orphaned traces carry cost
and latency that cannot be attributed to any ticket. This check surfaces
them automatically.

## How it works

1. **Short-circuits** when tracing is disabled (no per-repo Langfuse credentials in `config/repos.yaml`).

2. **Fetches all traces** from the last 24 hours via the Langfuse
   public API (paginated, with graceful error handling).

3. **Partitions** traces: those with a falsy `sessionId` (missing,
   `None`, or `""`) are "unsessioned."

4. **Skips silently** when there are zero unsessioned traces or zero
   total traces.

5. **Deduplicates:** queries the ticket table for any existing
   `source="trace-health"` ticket not in `CLOSED` state. If one
   exists, skips — an alert is already live.

6. **Files a single draft ticket** (`source="trace-health"`) with a
   structured body listing the window, the counts, up to 5 example
   trace IDs/names, and a note about the likely cause. The ticket
   flows through the normal pipeline (refine → approval gate →
   implement → PR → human merge).

The actual fix for session inheritance is a separate ticket the
pipeline will produce; this check is only the alert.

## Usage

**CLI:**
```sh
robotsix-mill trace-health              # summary output
robotsix-mill trace-health --json      # full JSON result
```

**API:**
```sh
curl -X POST http://localhost:8077/passes/trace_health/run
```

**Web board:** Open the `⚡ Passes` dropdown on the board page and select "Trace Health".

**Periodic polling (opt-in):**
```yaml
# In config/config.yaml:
periodic:
  trace_health:
    enabled: true
    interval_seconds: 86400  # 1 day
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MILL_TRACE_HEALTH_PERIODIC` | `false` | Enable periodic trace-health checks |
| `MILL_TRACE_HEALTH_INTERVAL_SECONDS` | `86400` | Seconds between automatic checks (minimum 3600) |

## Important notes

- The trace-health check does **NOT** use an LLM — it is pure data
  inspection (HTTP fetch + SQL query).
- The check does **NOT** fix the root cause (sub-agent span
  inheritance) — its only output is a draft alert ticket.
- The 24-hour lookback window is **hard-coded**, not configurable.
- The minimum periodic interval is **3600s (1 hour)**, enforced in
  the worker to avoid hammering Langfuse.
- When per-repo Langfuse credentials are not available (not configured in `config/repos.yaml`), the check is a zero-cost no-op.
- In multi-repo mode, the trace-health check runs independently for each
  registered repo — each with its own Langfuse project, ticket board, and
  deduplication scope.

## See also

- [index.md](index.md) — documentation home
- [docs/agents/index.md](agents/index.md) — agent catalog
- [docs/config/configuration.md](../config/configuration.md) — full env-var reference
