# Code scanning

The forge abstraction provides code scanning alert access through a
concrete (non-abstract) method on the `Forge` ABC.

## `list_code_scanning_alerts(*, source_branch: str) -> list[dict]`

Returns open code scanning alerts for a branch.

The base `Forge` class returns `[]` — code scanning is not required
for forge compliance. Only `GitHubForge` overrides this method.

## GitHub implementation

`GitHubForge.list_code_scanning_alerts()` calls:

```
GET /repos/{owner}/{repo}/code-scanning/alerts
    ?ref=refs/heads/{branch}
    &state=open
```

This fetches open CodeQL alerts via the GitHub security/code-scanning
API.

### Error handling

| HTTP status | Behaviour |
|-------------|-----------|
| 403 | Silently returns `[]` — App lacks `security_events:read` permission |
| 404 | Silently returns `[]` — no CodeQL configured on the repo |
| 200 | Returns parsed alert list |

### Alert shape

Each alert dict includes:
- `rule` — the CodeQL rule ID (e.g. `py/clear-text-logging-sensitive-data`)
- `most_recent_instance` — location info (path, line, message)
- `state` — always `"open"` (filtered at query time)
- `severity` — `"error"`, `"warning"`, `"note"`

## GitLab

`GitLabForge` does not override `list_code_scanning_alerts()` — it
inherits the base `[]` default. GitLab's SAST findings are reported
through the pipeline job artifacts rather than a dedicated alert API.

## Why this method exists

Code scanning alerts (CodeQL findings) live in GitHub's security API,
not in workflow job logs. Without this method, the CI-fix agent sees
only "CodeQL: failure" with no detail about which rules fired or where.

By providing the actual alert list (rule, path, line, message), the
`ci_fix` agent can address the **actual findings** instead of blindly
suppressing the check.

## Required permissions

The GitHub App needs **Code scanning alerts: Read** permission.
Without it, the API returns 403 and `list_code_scanning_alerts()`
silently returns `[]` — the CI-fix agent then has no findings to work
from and may add wrong or blind suppression comments.

## See also

- [architecture.md](architecture.md) — full forge design document (§2.5, §6.3 cover code scanning)
- [ci-monitoring.md](ci-monitoring.md) — CI monitoring integration
- [github-app.md](github-app.md) — App permission setup
