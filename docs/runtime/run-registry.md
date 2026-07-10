# Run Registry

The run registry (`runtime/run_registry.py`) is a durable, thread-safe
record of background pass executions. It tracks when each periodic pass
last ran, whether it succeeded or failed, and what it reported.

## Data model

Each entry is a `RunEntry` dataclass:

| Field | Type | Description |
|---|---|---|
| `id` | `str` | UUID hex, assigned at creation |
| `kind` | `str` | Pass kind label (e.g. `"audit"`, `"health"`, `"trace-health"`) |
| `started_at` | `str` | ISO-8601 UTC timestamp |
| `finished_at` | `str \| None` | ISO-8601 UTC timestamp (null while running) |
| `status` | `"running" \| "ok" \| "error"` | Current status |
| `summary` | `str` | Human-readable summary (set on `finish_ok`) |
| `error` | `str \| None` | Error detail (set on `finish_error`) |
| `repo_id` | `str` | Repo qualifier for per-repo passes |

Entries are capped at `MAX_ENTRIES = 50` most-recent on each save.

## Persistence

The registry persists to a JSON file on disk. Every mutation
(`start`, `finish_ok`, `finish_error`) triggers a flush. On load,
any entries left in `"running"` status (from a previous process that
terminated uncleanly) are reconciled to `"error"` with
`"interrupted by process restart"` — this prevents orphaned
"running" entries from hanging indefinitely in the board UI.

## Thread safety

A `threading.Lock` guards all reads and writes to both the in-memory
list and the JSON file. The registry is used from the async worker
loop and from synchronous periodic-pass threads.

## Public API

| Method | Description |
|---|---|
| `start(kind, repo_id="")` | Create a `"running"` entry, persist, return its id |
| `finish_ok(run_id, summary)` | Mark an entry as `"ok"` with a summary |
| `finish_error(run_id, error)` | Mark an entry as `"error"` with an error string |
| `most_recent(kind, repo_id=None)` | Return the newest successful entry of a kind |
| `list_all()` | Return all entries as dicts, newest first |

## Usage in the worker

The worker maintains one `RunRegistry` per repo (keyed by repo id) plus
a global registry for non-per-repo passes. Before running a periodic
pass, the worker checks `most_recent()` to determine whether the pass
is due; only `"ok"` entries count — an interrupted-by-restart or errored
run doesn't reset the timer, so the next fire window is measured from
the last *successful* execution.

The registry is surfaced to the UI via the `/health` endpoint so
operators can see recent pass results without accessing the data
directory directly.
