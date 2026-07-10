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
            "summary": str | None,    # capped at 2000 chars
            "text": str | None,       # capped at 4000 chars
            "annotations": [...]      # capped at 20 per check
        }
    ]
}
```

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
  `check_status()` returns `{"conclusion": "success", "failing": []}`.
  This prevents the merge stage from looping forever.
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
