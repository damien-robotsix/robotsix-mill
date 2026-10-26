# CI monitoring

The forge abstraction provides CI monitoring through two abstract
methods on the `Forge` ABC, with per-forge implementations.

## Methods

### `check_status(*, source_branch: str) -> dict | None`

Returns remote CI check-run/pipeline status for a PR/MR branch.

Returns `None` when no PR/MR exists. When one exists, returns:

```python
{
    "conclusion": "success" | "failure" | "pending" | None,
    "failing": [
        {
            "name": str,
            "summary": str | None,  # capped at 2000 chars
            "text": str | None,  # capped at 4000 chars
            "annotations": [...],  # capped at 20 per check
        }
    ],
    "pending": [str, ...],  # names of pending checks
    "jobs": [
        {"name": str, "conclusion": str | None}  # all jobs/checks, per-job detail
    ],
}
```

The `jobs` field is a flat list of every CI job/check for the commit,
each with its `name` and `conclusion` (`"success"`, `"failure"`,
`"pending"`, `"skipped"`, `"cancelled"`, or `None` when unknown).
This gives callers a complete per-job picture without needing an
external API call.

The `failing` list carries only checks whose conclusion is a genuine
failure (failure, timed_out, action_required, startup_failure), each
with detailed annotations and truncated log text. The `jobs` list
carries *every* check and its raw conclusion, making it easy to
identify which job failed (and what status the other jobs have) at
a glance. The `pending` list holds the names of checks that are
still in-flight or inconclusive (cancelled/stale — treated as pending
to avoid false CI-failure loops).

### `fetch_workflow_job_logs(*, run_id: int) -> str`

Returns concatenated, ANSI-stripped, size-capped logs of all failed
jobs in a workflow run/pipeline. Returns `""` when no failed jobs found.

### `list_workflow_runs(*, branch=None, head_sha=None) -> list[dict]`

Lists completed workflow/pipeline runs, optionally filtered by branch
or SHA. Each entry:

```python
{
    "id": int,
    "name": str,
    "workflow_id": int,
    "head_sha": str,
    "conclusion": str,
    "html_url": str,
    "created_at": str,
}
```

### `list_workflow_run_jobs(*, run_id: int) -> list[dict]`

Lists the jobs of a single workflow run. Returns a list of dicts, each
with `id`, `name`, `status`, and `conclusion`.

An **empty list** means the run never started a job — the signature of a
self-hosted queue timeout where GitHub auto-cancelled the run before any
runner picked it up. Callers use this to distinguish a queue-timeout
`cancelled` conclusion from a concurrency-cancelled run that did start
jobs.

```python
[
    {
        "id": int,
        "name": str,
        "status": str,  # e.g. "in_progress", "completed"
        "conclusion": str | None,  # e.g. "success", "failure", "cancelled"
    }
]
```

Concrete default returns `[]` — only GitHub implements this. Non-GitHub
forges inherit the no-op. May raise on transport error so callers can
distinguish "no jobs" from "could not tell".

### `has_online_runner() -> bool`

Returns `True` when at least one self-hosted runner for the repo is
online.

Reads `GET /repos/{owner}/{repo}/actions/runners` and checks for any
runner with `status == "online"`. A repo with no self-hosted runners
(e.g. a GitHub-hosted-only repo) returns `False` — which naturally
scopes runner-gated behavior (such as re-dispatching a queue-timed-out
publish) to self-hosted repos.

Concrete default returns `False` — only GitHub implements this. Must
**NEVER raise** — any API failure returns `False`.

### `get_workflow_triggers(*, path: str) -> list[str] | None`

Returns the top-level `on:` trigger event names of the workflow file
at *path* (e.g. `.github/workflows/refresh-deps.yml`).

Returns a list such as `["schedule", "workflow_dispatch"]`, or `None`
on any error (missing file, transport failure, unparseable YAML).

Used by the ci-auto-close pass to recognise a *cron-only* workflow:
one with `schedule` + `workflow_dispatch` triggers but no `push` /
`pull_request`, which never re-runs on its own after a fix merges to
the target branch.

### `dispatch_workflow(*, workflow_id=None, workflow_file=None, ref="main") -> dict`

Triggers a `workflow_dispatch` run of a workflow on a given ref
(branch/tag). Identify the workflow either by numeric `workflow_id`
(used by the CI-monitor queue-timeout re-dispatch) or by `workflow_file`
name (e.g. `refresh-deps.yml`, used by ci-auto-close) — GitHub's
dispatch endpoint accepts either form in the same path slot.

Returns `{"dispatched": True}` on success, `{"dispatched": False,
"reason": ...}` when the forge does not support manual dispatch, the
workflow lacks a `workflow_dispatch` trigger (GitHub returns HTTP 422),
or the operation otherwise fails.

```python
# Success
{"dispatched": True}

# Failure
{"dispatched": False, "reason": "no workflow_dispatch trigger"}
```

Concrete default returns `{"dispatched": False, "reason": "not
supported"}` — only GitHub implements this. Must **NEVER raise** — all
failures are caught and returned as a failure dict.

Used by ci-auto-close to re-trigger cron-only workflows after their
fix PR merges, preventing the stale red run from lingering for up to
a week until the next scheduled tick.

## GitHub implementation

`GitHubForge.check_status()` merges two GitHub API concepts:

- **Check runs** (`GET /repos/.../commits/{sha}/check-runs`) — GitHub
  Actions workflows and check-run integrations.
- **Statuses** (`GET /repos/.../commits/{sha}/status`) — legacy
  commit-status API, converted to check-run–shaped dicts.

A 403 from either endpoint (App lacks `checks:read` permission) is
treated as "no data from that source" — the method falls through
instead of failing.

**Job logs** are fetched via `GET /repos/.../actions/jobs/{job_id}/logs`,
filtered to failed-like conclusions (failure, cancelled, timed_out, etc.),
up to `_MAX_FAILED_JOBS = 10`.

**Workflow triggers** (`get_workflow_triggers()`) are fetched via
`GET /repos/.../contents/{path}`, parsing the response's base64-encoded
YAML to extract the `on:` block. Handles YAML 1.1's parsing of the bare
`on` key as boolean `True`.

**Workflow dispatch** (`dispatch_workflow()`) is triggered via
`POST /repos/.../actions/workflows/{file}/dispatches` with
`{"ref": ref}` in the request body. Uses exponential backoff retry for
401 (auth) failures.

## GitLab implementation

`GitLabForge.check_status()` fetches the latest pipeline for the MR:

1. `GET /projects/:id/merge_requests/:iid/pipelines?per_page=1`
2. If failed: `GET /projects/:id/pipelines/:pipeline_id/jobs?scope=failed&per_page=20`

**Job logs** are fetched via `GET /projects/:id/jobs/{job_id}/trace`,
filtered to failed jobs, up to `_MAX_FAILED_JOBS = 10`.

## Log truncation

`_capture_failure_window()` anchors log truncation on the **first**
failure marker rather than blindly tail-capping. This is necessary
because `if: always()` cascades in GitHub Actions cause downstream
steps to re-error with misleading input near the log tail.

Failure markers matched:
- `##[error]`
- `FATAL`
- `Error:`
- `exit code [1-9]`
- `Process completed with exit code [1-9]`

## Size caps

| Cap | Value | Applies to |
|-----|-------|-----------|
| `_MAX_FAILED_JOBS` | 10 | Jobs whose logs are fetched per run |
| `ci_log_max_bytes` | 65536 (default) | Max bytes per job log (after ANSI stripping) |
| Summary cap | 2000 chars | Per-check summary in `check_status` |
| Text cap | 4000 chars | Per-check detail text in `check_status` |
| Annotations cap | 20 | Per check in `check_status` |

## Edge cases

- **No CI configured**: When both check-runs and statuses are empty,
  `check_status()` returns `{"conclusion": "success", "failing": [],
  "pending": [], "jobs": []}`. This prevents the merge stage from
  looping forever.
- **Pending pipelines**: A pipeline/check that is still running yields
  `"conclusion": "pending"` — the merge stage polls again next cycle.
- **403 on checks endpoint**: Treated as "no data" rather than an error.
  Happens when the GitHub App lacks `checks:read` permission.
- **Branch deleted after merge**: `check_status()` by source branch
  returns `None` after the head branch is deleted. Callers should use
  `pr_status_by_url()` for post-merge status checks.

## See also

- [architecture.md](architecture.md) — full forge design document (§2.3, §3.5 cover CI in detail)
- [code-scanning.md](code-scanning.md) — code scanning alert integration
