# GitLab backend

The GitLab forge adapter (`src/robotsix_mill/forge/gitlab/`) implements
the `Forge` ABC against the GitLab REST API v4.

## Module layout

```
src/robotsix_mill/forge/gitlab/
├── __init__.py          # GitLabForge class, factory, exports
├── core.py              # MR/merge/review/branch operations
├── ci.py                # Pipeline & job-log operations
├── code_scanning.py     # Code scanning alerts (no-op for GitLab)
├── dependabot.py        # Dependency update MRs
└── _pagination.py       # Paginated list helpers
```

## Configuration

```yaml
# config/config.yaml
forge:
  kind: gitlab
  remote_url: https://gitlab.com/<namespace>/<project>.git
  target_branch: main
  auth_mode: token
secrets:
  forge_token: "<personal or project access token>"
```

### API URL override

For self-hosted GitLab instances:

```yaml
forge:
  gitlab_api_url: https://gitlab.example.com/api/v4
```

Or set `MILL_GITLAB_API_URL` as an environment variable.

## Key design differences from GitHub

### Auth header

GitLab uses `PRIVATE-TOKEN` instead of `Authorization: Bearer`:

```python
def _build_headers(token: str) -> dict:
    return {"PRIVATE-TOKEN": token}
```

### Project ID resolution

Every API call needs a numeric project ID. `_resolve_project_id()` calls
`GET /projects/:encoded_path` and caches the result. This is the single
resolution seam — all other `_http_*` methods call it first.

### MR lookup: branch-based vs. IID-based

- **`_find_mr(source_branch)`** — looks up by source branch. Returns
  `None` when the branch has been deleted (common after merge).
- **`_get_mr_by_iid(mr_iid)`** — looks up by IID (the project-scoped
  integer from the web URL). Survives branch deletion — the MR record
  persists.

### Pipeline status mapping

| GitLab status | Standard conclusion |
|---------------|-------------------|
| `success` | `"success"` |
| `failed`, `canceled` | `"failure"` |
| `pending`, `running`, `created`, `waiting_for_resource`, `preparing`, `manual`, `scheduled` | `"pending"` |
| anything else | `None` |

### Merge-status mapping

| GitLab `merge_status` | `mergeable` |
|----------------------|-------------|
| `can_be_merged` | `True` |
| `cannot_be_merged` | `False` |
| `checking`, `unchecked` | `None` (treat as mergeable) |

### Review-status heuristic

GitLab has no formal review-state object. `GitLabForge` uses a
simplified heuristic:

1. If `approved` is `true` on the MR approvals endpoint → `"APPROVED"`
2. Else if any non-system notes exist → `"COMMENTED"`
3. Else → `"PENDING"`

No per-comment state attribution is attempted (GitLab notes don't carry
a review-state field).

### Merge with MWPS

`merge_pr()` uses `merge_when_pipeline_succeeds=true` so the MR queues
automatically. On success, returns a dict with `"merged": true` and
`"reason": "merged"`.

### Diff counting

GitLab's `changes` endpoint does not report `additions`/`deletions`
separately. `_mr_changes()` counts `+`/`-` lines from the raw diff text.

### Shared utilities

`GitLabForge` imports `_ANSI_RE`, `_MAX_FAILED_JOBS`,
`_capture_failure_window`, and `_parse_iso_utc` from `github.py` to
avoid duplication.

## See also

- [architecture.md](architecture.md) — full forge design document (§3 covers GitLab in detail)
- [auth.md](auth.md) — authentication setup
- [ci-monitoring.md](ci-monitoring.md) — CI monitoring across forges
