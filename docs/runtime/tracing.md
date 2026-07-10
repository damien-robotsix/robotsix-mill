# Tracing

The runtime includes optional OpenTelemetry tracing to Langfuse,
implemented in `runtime/tracing.py`. When Langfuse credentials are
configured, every stage run is traced; when they are absent, the entire
tracing layer is a cheap no-op.

## Architecture

The OTLP→Langfuse plumbing — the global `TracerProvider`, the
`OTLPSpanExporter`, `Agent.instrument_all(...)` instrumentation, and
session/project contextvars — lives in `robotsix_llmio.core.tracing`.
Mill delegates provider/exporter setup and session/project context to
llmio and keeps only the mill-specific surface:

- **Per-repo credential resolution** — each `RepoConfig` can supply its
  own Langfuse public/secret keys, allowing multiple repos to share a
  single mill process while routing traces to separate Langfuse
  projects.
- **Export-failure registry** — a ring buffer of recent export failures
  surfaced to the UI via `/health/langfuse-status`.
- **Session id qualification** — repo-prefixed session ids
  (e.g. `robotsix-llmio · <ticket-id>`) so a shared Langfuse project's
  session list is legible.
- **Signal handlers** — SIGTERM/SIGINT handlers flush pending spans
  before the process exits.

## Key functions

| Function | Purpose |
|---|---|
| `start_ticket_root_span(ticket_id, stage_name)` | Opens a root OTel span for one stage run. Yields a `_RootIO` handle for attaching input/output payloads. |
| `trace_stage(stage_name)` | Creates a child span under the current active span for sub-operations. |
| `record_step_usage(...)` | Records per-turn token counts and tool calls as span attributes for cost analysis. |
| `flush_tracing(timeout)` | Force-flushes pending spans at shutdown. |
| `make_session_id(kind)` | Builds a Langfuse session id for non-ticket flows (audit, health, etc.). |
| `current_ticket_id()` | Returns the bare ticket id from the active Langfuse session, stripping the repo qualifier. |
| `langfuse_trace_url(trace_id)` | Builds the Langfuse web-UI URL for a trace. |

## Credential resolution

`_ensure_tracing(repo_config)` lazily configures tracing per Langfuse
public key. It is idempotent: the first call for a key configures
llmio's global provider; later calls for the same key short-circuit.
Repos without credentials are skipped silently.

## Export failure tracking

`record_export_failure()` appends entries to a capped ring buffer
(`_EXPORT_FAILURE_CAP = 20`). The `/health/langfuse-status` endpoint
reads this buffer so operators notice when traces aren't making it
through. Successful exports automatically clear failure entries for
the corresponding project via `clear_export_failures_for()`.

## Span attributes

The `_RootIO` handle lets callers attach human-readable input/output
payloads to the root span (JSON-serialized, capped at 8,000 chars).
Langfuse reads `langfuse.observation.input`/`output` and renders them
at the trace level. `record_step_usage()` stamps per-turn aggregates
as `langfuse.observation.metadata.mill.step_usage` for the cost
analyst and trace inspector.

## No-op safety

Every function in `tracing.py` guards behind `_provider_ready` and lazy
imports — zero imports from `opentelemetry.*`, `langfuse`, or
`robotsix_llmio` at module level. When credentials are absent, every
call is a cheap no-op that returns `None` or a no-op context manager.
