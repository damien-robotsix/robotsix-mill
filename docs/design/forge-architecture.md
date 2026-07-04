# Forge Architecture — Design Document

> **Status:** draft  
> **Epic:** gitlab-forge-support  
> **Scope:** design only — no code changes in this ticket  

## Overview

The forge abstraction layer (`src/robotsix_mill/forge/`) isolates the
deliver, merge, and CI-monitor stages from concrete forge APIs.  Two
adapters ship today: `GitHubForge` (GitHub REST API) and `GitLabForge`
(GitLab REST API v4).  A factory function `get_forge()` dispatches on
`Settings.forge_kind`.

This document describes the interface contract, each adapter's API
mapping, the authentication layer, the configuration surface, key
design decisions, and how to add a third provider.

---

## 1. Forge interface contract

### 1.1 Data classes

```python
@dataclass
class RepoInfo:
    id: int
    name: str
    clone_url: str
    html_url: str

@dataclass
class BranchInfo:
    name: str
    last_commit_at: datetime   # timezone-aware (UTC)
    is_protected: bool
```

### 1.2 ABC: `Forge`

**Module:** `src/robotsix_mill/forge/base.py`

The constructor receives `settings: Settings` and stores it as
`self.settings`.

#### 1.2.1 Abstract methods (12 total)

Every adapter must implement these.  Return shapes are normative —
callers in the deliver/merge stages depend on these exact keys.

| # | Method | Signature | Returns | Semantics |
|---|--------|-----------|---------|-----------|
| 1 | `open_merge_request` | `(*, source_branch: str, title: str, body: str)` | `str` | Open an MR/PR for `source_branch` against `settings.forge_target_branch`.  Returns the MR/PR web URL.  Must handle the "already exists" case by returning the existing URL instead of failing. |
| 2 | `pr_status` | `(*, source_branch: str)` | `dict | None` | Status by head branch.  Returns `{"merged": bool, "state": "open"|"closed", "url": str, "mergeable": bool|None}` or `None` if no PR exists. `mergeable` is `None` when the forge hasn't computed it yet (treat as mergeable). |
| 3 | `pr_status_by_url` | `(*, url: str)` | `dict | None` | Status by recorded PR URL, independent of whether the head branch still exists.  Same return shape as `pr_status`, plus a `"number"` key (the forge-native MR/PR number or IID).  Returns `None` when the URL cannot be parsed or the PR cannot be resolved. |
| 4 | `check_status` | `(*, source_branch: str)` | `dict | None` | Remote CI check-run status.  Returns `None` when no PR exists.  When a PR exists: `{"conclusion": "success"|"failure"|"pending"|None, "failing": [...]}`.  Each failing entry: `{"name": str, "summary": str|None, "text": str|None, "annotations": [...]}`.  Summaries capped at 2000 chars, text at 4000, annotations at 20 per check. |
| 5 | `pr_files` | `(*, source_branch: str)` | `list[dict]` | File-list diff of the PR.  `[]` when no PR exists.  Each dict: `"path"` (str), `"status"` (`"added"|"modified"|"removed"|"renamed"`), `"additions"` (int), `"deletions"` (int). |
| 6 | `pr_review_status` | `(*, source_branch: str)` | `dict | None` | Aggregate review state.  `None` when no PR exists.  Returns `{"state": "APPROVED"|"CHANGES_REQUESTED"|"COMMENTED"|"DISMISSED"|"PENDING", "comments": [...], "files": [...]}`.  `comments` entries: `"body"`, `"path"`, `"line"` (int|None), `"review_state"`.  `files` is a plain list of path strings. |
| 7 | `merge_pr` | `(*, source_branch: str)` | `dict` | Squash-merge the PR.  Returns `{"merged": bool, "reason": str}`.  Must never raise for API-level failures — catch and return a failure dict. |
| 8 | `list_pr_reviews` | `(*, source_branch: str)` | `list[dict]` | Formal PR reviews (approve/request-changes/comment).  `[]` when no PR exists.  Each: `"id"`, `"author"`, `"created_at"`, `"body"` (never `None`, `""` when submitted without body). |
| 9 | `list_review_comments` | `(*, source_branch: str)` | `list[dict]` | Inline code-review comments.  `[]` when no PR exists.  Each: `"id"`, `"author"`, `"created_at"`, `"body"`, `"file_path"`, `"line"` (int|None), `"diff_hunk"`. |
| 10 | `list_workflow_runs` | `(*, branch: str|None, head_sha: str|None)` | `list[dict]` | Completed workflow/pipeline runs, optionally filtered.  Each: `"id"`, `"name"`, `"workflow_id"`, `"head_sha"`, `"conclusion"`, `"html_url"`, `"created_at"`. |
| 11 | `fetch_workflow_job_logs` | `(*, run_id: int)` | `str` | Concatenated, ANSI-stripped, size-capped logs of all failed jobs in a workflow run.  Returns `""` when no failed jobs found. |
| 12 | `create_repo` | `(*, name: str, owner: str, private: bool, description: str)` | `RepoInfo` | Create a new repository.  Must raise `NotConfiguredError` when repo creation is disabled by configuration. |

#### 1.2.2 Concrete (non-abstract) methods (4 total)

These have default implementations in the base class.  Adapters **may**
override them when the forge supports the capability.

| Method | Default behaviour | GitHub override | GitLab override |
|--------|-------------------|-----------------|-----------------|
| `list_code_scanning_alerts(*, source_branch)` | Returns `[]` — code-scanning is GitHub-only. | Fetches open CodeQL alerts via the security/code-scanning API. | Not overridden (inherits `[]`). |
| `update_branch(*, source_branch)` | Returns `{"updated": False, "reason": "not supported"}` — MR/PR branch rebasing is not implemented by default. | `PUT /repos/{owner}/{repo}/pulls/{number}/update-branch` — merges the target branch tip into the PR branch so CI re-runs against the current base. | `PUT /projects/:id/merge_requests/:iid/rebase` — rebases the MR branch against the target branch; returns `{"updated": True, "reason": "rebase accepted"}` on 202 (accepted). |
| `delete_branch(*, branch)` | Returns `False`. | `DELETE /repos/.../git/refs/heads/{branch}` — returns `True` on 204/404/422 (branch gone = success). | `DELETE /projects/:id/repository/branches/:branch` — returns `True` on 204/404. |
| `list_branches()` | Returns `[]`. | Paginates `GET /repos/.../branches`, returns `list[BranchInfo]`. | Paginates `GET /projects/:id/repository/branches`. |
| `list_open_pr_branches()` | Returns `set()` (empty). | Paginates `GET /repos/.../pulls?state=open`, collects head refs. | Paginates `GET /projects/:id/merge_requests?state=opened`, collects `source_branch`. |

### 1.3 Factory: `get_forge()`

```python
def get_forge(settings: Settings, repo_config: RepoConfig | None = None) -> Forge:
    kind = settings.forge_kind
    if kind == "auto":
        remote_url = (
            (repo_config.forge_remote_url if repo_config is not None else None)
            or settings.forge_remote_url
            or ""
        )
        kind = _detect_forge_kind(remote_url)
    if kind == "github":
        return GitHubForge(settings, repo_config=repo_config)
    if kind == "gitlab":
        return GitLabForge(settings, repo_config=repo_config)
    raise RuntimeError(f"no forge configured (FORGE_KIND={kind!r}); cannot deliver")
```

When `repo_config` is provided, the adapter uses its
`forge_remote_url` instead of the global `Settings.forge_remote_url`,
so different repos under the same forge can target different remotes.

When `forge_kind` is `"auto"`, the effective forge kind is detected
from the remote URL (per-repo `forge_remote_url` takes precedence over
the global setting). See §5.3 for the auto-detection heuristics.

### 1.4 `NotConfiguredError`

```python
class NotConfiguredError(RuntimeError):
    """Raised when an optional forge capability (e.g. repo creation) is
    disabled by configuration."""
```

Used by `create_repo()` when `enable_repo_creation` is `False`.

---

## 2. GitHub implementation

**Module:** `src/robotsix_mill/forge/github.py`  
**Class:** `GitHubForge(Forge)`

### 2.1 Remote URL resolution

The `_owner_repo` property parses the effective remote URL (per-repo
override or global `forge_remote_url`) via `_parse_owner_repo()`, which
extracts `(owner, repo)` from patterns like
`github.com[:/]<owner>/<repo>[.git]`.

### 2.2 Abstract method → REST endpoint mapping

Each public method resolves `owner`/`repo`, then delegates to a private
`_http_*` method (the **HTTP seam** — see §6.1).

| Method | HTTP seam | GitHub REST endpoint |
|--------|-----------|---------------------|
| `open_merge_request` | `_create_pr` | `POST /repos/{owner}/{repo}/pulls` with 422 retry (4 attempts, exponential backoff 1s/2s/4s). On 422, checks `GET /repos/.../pulls?head={owner}:{head}&state=open` for an existing PR. A 422 with `"field":"head","code":"invalid"` is treated as a transient post-push indexing race and retried. |
| `pr_status` | `_get_pr` | `GET /repos/{owner}/{repo}/pulls?head={owner}:{head}&state=all` → pick first item → `GET /repos/.../pulls/{number}` |
| `pr_status_by_url` | `_get_pr_by_number` | Parses PR number from URL (`/pull/(\d+)`), then `GET /repos/{owner}/{repo}/pulls/{number}` |
| `check_status` | `_check_status` | 1. `_get_pr` for SHA. 2. `GET /repos/.../commits/{sha}/check-runs` (403 → fall through). 3. `GET /repos/.../commits/{sha}/status` (403 → fall through). 4. Combine check runs + statuses. 5. For each failing check run, `GET /repos/.../check-runs/{id}` for annotations. |
| `pr_files` | `_pr_files` | `GET /repos/{owner}/{repo}/pulls/{number}/files?per_page=100` |
| `pr_review_status` | `_pr_review_status` | `GET /repos/.../pulls/{number}/reviews` + `GET /repos/.../pulls/{number}/comments` + `_pr_files`. Aggregate state derived from **latest non-dismissed review** (fallback: latest dismissed). |
| `merge_pr` | `_merge_pr` | `PUT /repos/{owner}/{repo}/pulls/{number}/merge` with `merge_method=squash`. 200 → merged; 405 → branch protection; 409 → not mergeable. |
| `list_pr_reviews` | `_list_pr_reviews` | `GET /repos/{owner}/{repo}/pulls/{number}/reviews?per_page=100` |
| `list_review_comments` | `_list_review_comments` | `GET /repos/{owner}/{repo}/pulls/{number}/comments?per_page=100` |
| `list_workflow_runs` | `_list_workflow_runs` | `GET /repos/{owner}/{repo}/actions/runs?status=completed&per_page=30` (optional `branch`/`head_sha` params) |
| `fetch_workflow_job_logs` | `_fetch_workflow_job_logs` | `GET /repos/.../actions/runs/{run_id}/jobs?status=completed` → filter to failed-like conclusions → `GET /repos/.../actions/jobs/{job_id}/logs` for each (up to `_MAX_FAILED_JOBS`) |
| `create_repo` | `_create_repo` | `POST /orgs/{owner}/repos` → fallback `POST /user/repos` on 403/404. 422 with "name already exists": attempt to reuse if repo is empty (no commits). |

### 2.3 `check_status` detail

The method merges two GitHub API concepts:

- **Check runs** (`/commits/{sha}/check-runs`) — GitHub Actions workflows
  and any check-run integration.
- **Statuses** (`/commits/{sha}/status`) — legacy commit-status API,
  converted to check-run–shaped dicts by `_statuses_to_check_runs()`.

A 403 from either endpoint (App lacks `checks:read` permission) does
not fail — the method treats it as "no data from that source" and
falls through.

Conclusions: `_derive_check_conclusion()` classifies each check as
`pending`/`failure`/`neutral`.  Overall is `"failure"` if any check
failed, `"pending"` if any check is in-flight, `"success"` if all
completed and passed, `None` if no checks exist at all.  When **both**
check-runs and statuses are empty (no CI configured), the method
returns `{"conclusion": "success", "failing": []}` so the merge stage
doesn't loop forever.

### 2.4 `mergeable` normalization

`_parse_pr_detail()` maps GitHub's `mergeable_state` + `mergeable`
into the standard `mergeable` field:

| `mergeable_state` | `mergeable` field |
|-------------------|-------------------|
| `null` or `"unknown"` | `None` (still computing) |
| `"clean"` | `True` |
| `"dirty"`, `"blocked"`, `"unstable"`, … | `False` (from `mergeable`) |

When `mergeable_state` is `"unknown"`, the caller considers the PR
mergeable and polls again next cycle.

### 2.5 Code scanning alerts

`list_code_scanning_alerts()` is **not** abstract — the base class
returns `[]`.  `GitHubForge` overrides it to call
`GET /repos/{owner}/{repo}/code-scanning/alerts?ref=refs/heads/{branch}&state=open`.
403 (no security-events scope) and 404 (no CodeQL) degrade silently
to `[]`.

### 2.6 Repo creation reuse safety

`_create_repo()`'s 422 "name already exists" path does not
unconditionally fail.  `_reuse_if_empty()` checks whether the existing
repo has zero commits (`GET /repos/.../commits` returns 409 or empty
list).  An empty repo is treated as a prior incomplete scaffold attempt
and reused; a repo with real content raises a `RuntimeError`.

### 2.7 Utility functions

| Function | Purpose |
|----------|---------|
| `_ANSI_RE` | Compiled regex stripping ANSI escape sequences from CI logs. |
| `_MAX_FAILED_JOBS = 10` | Cap on number of failed jobs whose logs are fetched per run. |
| `_capture_failure_window(clean_log, max_bytes)` | Anchors log truncation on the **first** failure marker so `if: always()` cascades can't mask the real error. |
| `_LOG_FAILURE_RE` | Regex matching failure markers (`##[error]`, `FATAL`, `Error:`, `exit code [1-9]`, `Process completed with exit code [1-9]`). |
| `_parse_iso_utc(value)` | Parses ISO-8601 timestamps (with `Z` support) into timezone-aware UTC `datetime`. Returns Unix epoch on failure. |
| `_parse_pr_detail(pr)` | Normalizes a GitHub PR dict into the standard status shape. |
| `_build_headers(token)` | Returns `{"Authorization": "Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}`. |
| `_parse_owner_repo(remote_url)` | Extracts `(owner, repo)` from GitHub remote URLs. |
| `_clamp_repo_description(description)` | Truncates to 350 chars (GitHub's repo-description limit). |
| `_parse_repo_info(r)` | Extracts `RepoInfo` from a GitHub repo-creation response. |

`_ANSI_RE`, `_MAX_FAILED_JOBS`, `_capture_failure_window`, and
`_parse_iso_utc` are **re-used by `GitLabForge`** (imported from
`github.py`).

---

## 3. GitLab implementation

**Module:** `src/robotsix_mill/forge/gitlab.py`  
**Class:** `GitLabForge(Forge)`

### 3.1 Remote URL → project path

`_parse_gitlab_project_path(remote_url)` extracts the namespace/project
path from both HTTPS (`https://<host>/ns/project.git`) and SSH
(`git@<host>:ns/project.git`) URLs.

### 3.2 Project ID resolution — the central seam

Every GitLab API call needs a numeric project ID.  `_resolve_project_id(project_path)`
calls `GET /projects/:encoded_path` (URL-encoded path) and returns the
project `id` field.  This is the single point of project-ID resolution
— all other `_http_*` methods call it first.

### 3.3 Abstract method → REST endpoint mapping

| Method | HTTP seam | GitLab REST endpoint |
|--------|-----------|---------------------|
| `open_merge_request` | `_create_mr` | `POST /projects/:id/merge_requests` with `source_branch`, `target_branch`, `title`, `description`. On 409 (conflict → MR already exists), calls `_find_mr` to return the existing MR's `web_url`. |
| `pr_status` | `_find_mr` | `GET /projects/:id/merge_requests?source_branch=...&state=all&per_page=1` |
| `pr_status_by_url` | `_get_mr_by_iid` | Parses MR IID from URL (`merge_requests/(\d+)`), then `GET /projects/:id/merge_requests/:iid` |
| `check_status` | `_get_latest_pipeline` + `_get_failed_jobs` | 1. `_find_mr` for the MR. 2. `GET /projects/:id/merge_requests/:iid/pipelines?per_page=1` for the latest pipeline. 3. If failed: `GET /projects/:id/pipelines/:pipeline_id/jobs?scope=failed&per_page=20` |
| `pr_files` | `_mr_changes` | `GET /projects/:id/merge_requests/:iid/changes` — counts `+`/`-` lines from the raw diff text |
| `pr_review_status` | `_pr_review_status` | `GET /projects/:id/merge_requests/:iid/approvals` (approval state) + `_mr_notes` (comments). Heuristic: `"APPROVED"` when approved, `"COMMENTED"` when any non-system notes exist, else `"PENDING"`. |
| `merge_pr` | `_merge_mr` | `PUT /projects/:id/merge_requests/:iid/merge` with `merge_when_pipeline_succeeds=true`, `squash=true`, `should_remove_source_branch=false`. 200 → merged or MWPS-queued; 405 → branch protection; 409 → not mergeable. |
| `list_pr_reviews` | `_mr_notes` | `GET /projects/:id/merge_requests/:iid/notes?per_page=100` — returns non-system notes **without** a `position` (general comments). |
| `list_review_comments` | `_mr_notes` | Same endpoint — returns notes **with** a `position` (inline diff comments). |
| `list_workflow_runs` | `_list_pipelines` | `GET /projects/:id/pipelines?per_page=30` (optional `ref`/`sha` params) |
| `fetch_workflow_job_logs` | `_fetch_pipeline_job_logs` | `GET /projects/:id/pipelines/{run_id}/jobs?scope=failed&per_page=20` → `GET /projects/:id/jobs/{job_id}/trace` for each (up to `_MAX_FAILED_JOBS`) |
| `create_repo` | `_create_project` | `POST /projects` with optional `namespace_id` (resolved via `GET /namespaces/:encoded`). 400/409 "already been taken" / "already exists" raises `RuntimeError`. |

### 3.4 MR lookup: branch-based vs. IID-based

- **`_find_mr(project_path, source_branch, state)`** — looks up an MR by
  `source_branch`.  Used by `pr_status`, `check_status`, `pr_files`,
  `merge_pr`, `list_pr_reviews`, and `list_review_comments`.  Returns
  `None` when the source branch has been deleted (common after merge).

- **`_get_mr_by_iid(project_path, mr_iid)`** — looks up an MR by its IID
  (the project-scoped integer in the web URL).  Used by
  `pr_status_by_url` to survive branch deletion — the MR record
  persists after the head branch is gone.

### 3.5 Pipeline status mapping

`_map_pipeline_status()` maps GitLab pipeline statuses to the standard
conclusion:

| GitLab status | Standard conclusion |
|---------------|-------------------|
| `success` | `"success"` |
| `failed`, `canceled` | `"failure"` |
| `pending`, `running`, `created`, `waiting_for_resource`, `preparing`, `manual`, `scheduled` | `"pending"` |
| anything else | `None` |

### 3.6 Merge-status mapping

`_map_merge_status()` maps GitLab's `merge_status` field to the
standard `mergeable` boolean-or-`None`:

| GitLab `merge_status` | `mergeable` |
|----------------------|-------------|
| `can_be_merged` | `True` |
| `cannot_be_merged` | `False` |
| `checking`, `unchecked`, anything else | `None` (treat as mergeable) |

### 3.7 Review-status heuristic

GitLab has no formal review-state object equivalent to GitHub's
reviews.  `GitLabForge._pr_review_status()` uses a simplified heuristic:

1. `GET /projects/:id/merge_requests/:iid/approvals` — if `approved` is
   `true`, state is `"APPROVED"`.
2. Otherwise, if any non-system notes exist, state is `"COMMENTED"`.
3. Otherwise, `"PENDING"`.

All comments (non-system notes) are returned with the same
`review_state` as the aggregate.  This is the simpler of the two
possible designs — the codebase chose not to attempt per-note
state attribution (GitLab notes don't carry a review-state field).

### 3.8 MR changes diff parsing

`_mr_changes()` calls `GET /projects/:id/merge_requests/:iid/changes`
and counts `+`/`-` lines from the raw diff text (GitLab's `changes`
endpoint does not report `additions`/`deletions` counts separately).

### 3.9 Auth header

```python
def _build_headers(token: str) -> dict:
    return {"PRIVATE-TOKEN": token}
```

GitLab uses the `PRIVATE-TOKEN` header (not `Authorization: Bearer`).

---

## 4. Authentication

**Module:** `src/robotsix_mill/forge/auth.py`

### 4.1 Two modes

| Mode | `FORGE_AUTH` value | Behaviour |
|------|-------------------|-----------|
| **Static token** | `token` (default) | Reads `Secrets.forge_token` directly.  Works for both GitHub (PAT) and GitLab (personal/project access token). |
| **GitHub App** | `app` | Mints a short-lived GitHub App installation token via JWT → installation lookup → access token.  **GitHub-only** — GitLab has no App concept. |

### 4.2 Static token (`FORGE_AUTH=token`)

```
Settings.forge_auth = "token"
Secrets.forge_token  = "<PAT or access token>"
```

- GitHub: sent as `Authorization: Bearer {token}`.
- GitLab: sent as `PRIVATE-TOKEN: {token}`.

The token is read from `Secrets.forge_token` (populated from
the `config/config.json` `secrets:` block).  `Settings.forge_token` (aliased `FORGE_TOKEN`
env var) exists in the Settings model but is **not** the runtime source
— the code always calls `get_secrets().forge_token`.

If `forge_token` is empty, `github_token()` raises `RuntimeError`.

### 4.3 GitHub App (`FORGE_AUTH=app`)

```
Settings.forge_auth = "app"
Secrets.github_app_id              = "<App ID>"
Secrets.github_app_private_key      = "<PEM key>"       # OR
Secrets.github_app_private_key_path = "/path/to/key.pem"
```

Flow in `_mint_installation_token()`:

1. **JWT creation** — signed with the App private key (RS256), expires
   in 9 minutes.  Issuer is the App ID.
2. **Installation lookup** — `GET /repos/{owner}/{repo}/installation`
   with `Authorization: Bearer {jwt}` returns the installation ID.
3. **Access token** — `POST /app/installations/{iid}/access_tokens`
   returns a short-lived token (GitHub issues ~1 hour).
4. **Caching** — tokens are cached in `_cache: dict[str, tuple[str, float]]`
   keyed by `"{app_id}:{remote_url}"`.  Cache TTL is 50 minutes
   (checked: if `cached[1] - 60 > time.time()`).  This ensures one
   deliver (push + PR) doesn't mint twice.

`_mint_installation_token` is a **test seam** — tests monkeypatch it
to avoid the real JWT/network flow.

### 4.4 Per-repo installation token minting

When a `RepoConfig` with a non-empty `forge_remote_url` is passed,
`_resolve_remote_url()` uses that repo's remote instead of the global
setting.  The cache key includes the remote URL, so different repos
under the same App get separate cached tokens.

### 4.5 `Secrets.forge_repo_create_token`

A separate PAT used **only** for `create_repo()`.  GitHub App
installation tokens cannot create repositories under personal accounts.
When `forge_repo_create_token` is set, it is used instead of the main
token for `POST /orgs/{owner}/repos` and `POST /user/repos`.  Falls
back to the normal `github_token()` when not set.

---

## 5. Configuration surface

### 5.1 `Settings` fields

All fields are in `src/robotsix_mill/config.py`, class `Settings`.

| Field | Type | Default | Env/YAML alias | Purpose |
|-------|------|---------|----------------|---------|
| `forge_kind` | `Literal["github","gitlab","auto","none"]` | `"none"` | `FORGE_KIND` | Which forge adapter to use. `"none"` disables forge delivery entirely. `"auto"` detects the forge kind from the remote URL hostname (see §5.3). |
| `forge_remote_url` | `str \| None` | `None` | `FORGE_REMOTE_URL` | Remote URL of the target repository (used for clone, push, and API calls). |
| `forge_target_branch` | `str` | `"main"` | `FORGE_TARGET_BRANCH` | Target branch for PRs/MRs. |
| `forge_auth` | `Literal["token","app"]` | `"token"` | `FORGE_AUTH` | Auth mode: `"token"` for static PAT, `"app"` for GitHub App JWT flow. |
| `forge_token` | `str \| None` | `None` | `FORGE_TOKEN` | Static forge token (env-var override; runtime source is `Secrets.forge_token`). |
| `github_app_id` | `str \| None` | `None` | `GITHUB_APP_ID` | GitHub App ID (env-var override; runtime source is `Secrets.github_app_id`). |
| `github_app_private_key` | `str \| None` | `None` | `GITHUB_APP_PRIVATE_KEY` | GitHub App private key (env-var override; runtime source is `Secrets.github_app_private_key`). |
| `github_app_private_key_path` | `str \| None` | `None` | `GITHUB_APP_PRIVATE_KEY_PATH` | Host path to the private key `.pem` file (not a secret — path may be in YAML). |
| `github_api_url` | `str` | `"https://api.github.com"` | — | API base URL (override for GitHub Enterprise Server). |
| `gitlab_api_url` | `str` | `"https://gitlab.com/api/v4"` | — | API base URL (override for self-hosted GitLab instances). |
| `enable_repo_creation` | `bool` | `False` | — | Gate for `create_repo()`. Must be explicitly enabled. |
| `ci_log_max_bytes` | `int` | `65536` | — | Maximum bytes fetched per CI job log (applied after ANSI stripping). |
| `delete_branch_on_merge` | `bool` | `True` | — | Delete the head branch after merge. |

Stale-branch cleanup fields (forge-adjacent, not core to the forge
abstraction): `stale_branch_cleanup_periodic`, `stale_branch_cleanup_interval_seconds`,
`stale_branch_max_age_days`, `stale_branch_cleanup_prefix_only`.

### 5.2 `Secrets` fields

All fields are in `src/robotsix_mill/config.py`, class `Secrets`.
Loaded from the `config/config.json` `secrets:` block (overridable via `MILL_SECRETS_FILE`).
Singleton, accessed via `get_secrets()`.

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `forge_token` | `str \| None` | `None` | Static forge access token (PAT for GitHub, personal/project token for GitLab). |
| `forge_repo_create_token` | `str \| None` | `None` | Separate PAT for `create_repo()` when the main token lacks sufficient scope. |
| `github_app_id` | `str \| None` | `None` | GitHub App ID (needed for `FORGE_AUTH=app`). |
| `github_app_private_key` | `str \| None` | `None` | GitHub App private key PEM text. |
| `github_app_private_key_path` | `str \| None` | `None` | Path to the private key `.pem` file (may be in secrets or YAML). |

### 5.3 Auto-detection (`auto` mode)

When `forge_kind` is `"auto"`, the system inspects the remote URL
hostname to decide which forge adapter to use.

**Resolution order:**
1. If a per-repo `forge_remote_url` is set on the `RepoConfig`, use that URL.
2. Otherwise, fall back to the global `Settings.forge_remote_url`.
3. If neither is set, the empty string is passed to `_detect_forge_kind()`,
   which raises `RuntimeError`.

**Heuristics** (in `_detect_forge_kind()`):
- Host `github.com` → `"github"` (covers `https://github.com/...` and `git@github.com:...`)
- Host `gitlab.com` → `"gitlab"` (covers `https://gitlab.com/...` and `git@gitlab.com:...`)
- Any other host (GitHub Enterprise Server, self-hosted GitLab, etc.) → raises
  `RuntimeError` with a message instructing the operator to set `FORGE_KIND`
  explicitly.

**Error case:** Custom domains are ambiguous — there is no way to
distinguish a GitHub Enterprise Server instance from a self-hosted
GitLab instance by hostname alone. The operator must set
`FORGE_KIND=github` or `FORGE_KIND=gitlab` explicitly.

### 5.4 `RepoConfig` field

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `forge_remote_url` | `str \| None` | `None` | Per-repo override of `Settings.forge_remote_url`. When set, the forge adapter targets this repo's remote instead of the global one. |

### 5.5 YAML defaults — `forge:` block

In `config/config.example.json`:

```yaml
forge:
  kind: none                              # github | gitlab | auto | none
  remote_url: null                        # remote URL for clone + push
  target_branch: main                     # target branch for PRs
  auth_mode: token                        # token (PAT) | app (GitHub App)
  github_app_private_key_path: null       # host path to App private-key .pem (not a secret)
  github_api_url: https://api.github.com  # override for GitHub Enterprise
  gitlab_api_url: https://gitlab.com/api/v4  # override for self-hosted GitLab
```

`github_app_id` and `github_app_private_key` are **not** in the YAML
defaults — they are secrets and belong in the `config.json` `secrets:` block.

`ci_log_max_bytes` is under `periodic.ci_monitor.log_max_bytes` in the
YAML (used by the CI-monitor periodic agent as well as by
`fetch_workflow_job_logs`).

---

## 6. Key design decisions

### 6.1 HTTP seam pattern

Every network call lives in a private `_http_*` method (e.g.
`_create_pr`, `_get_pr`, `_get_latest_pipeline`).  Tests do **not** mock
`httpx` at the module level — they monkeypatch these specific methods on
a live `GitHubForge`/`GitLabForge` instance, replacing them with
functions that return canned responses.  This avoids all real network
calls while testing the full call chain from public method → owner/repo
resolution → HTTP seam → response parsing.

The auth token functions (`github_token`, `_mint_installation_token`)
are also monkeypatched in tests.

### 6.2 `pr_status_by_url` — IID-based lookup (GitLab)

GitLab's `_get_mr_by_iid` resolves an MR by its IID (the project-scoped
integer from the web URL) rather than by source branch.  This is
deliberate: after a merge, the merge stage may delete the head branch
(especially when `delete_branch_on_merge` is `True` or the forge
auto-deletes).  Branch-based lookup (`_find_mr`) returns `None` for a
deleted branch, but IID-based lookup survives because the MR record
persists.  GitHub's equivalent is `_get_pr_by_number`, which fetches a
PR directly by its repository-scoped number.

### 6.3 `list_code_scanning_alerts` — GitHub-only, no-op default

Code scanning alerts (CodeQL) are a GitHub-specific feature accessed
via the security/code-scanning API.  The base `Forge` class provides a
concrete `[]` default so non-GitHub adapters don't need to implement
it.  Only `GitHubForge` overrides it.  The feature exists because
CodeQL findings live in the security API, not in workflow job logs —
without it the CI-fix agent sees only "CodeQL: failure" with no detail.

### 6.4 `_MAX_FAILED_JOBS = 10` and `_capture_failure_window`

`_MAX_FAILED_JOBS` caps the number of failed jobs whose logs are
fetched per workflow run to 10 — a run with 50 failed matrix jobs won't
flood the agent context.

`_capture_failure_window()` anchors log truncation on the **first**
failure marker rather than blindly tail-capping.  This is necessary
because GitHub Actions `if: always()` cascades cause downstream steps to
re-error with misleading input near the log tail.  Anchoring on the
earliest `##[error]`, `FATAL`, `Error:`, `exit code [1-9]`, or
`Process completed with exit code [1-9]` preserves the true root cause.

### 6.5 Why `forge_token` exists in both `Settings` and `Secrets`

`Settings.forge_token` (aliased `FORGE_TOKEN`) allows environment-level
overrides through the standard YAML pipeline.  `Secrets.forge_token` is
the canonical runtime source (loaded from the `config.json` `secrets:` block).  All forge
code calls `get_secrets().forge_token` — the `Settings` field is for
configuration ergonomics but is not the runtime read path.

---

## 7. GitHub ↔ GitLab API mapping table

For every method in the `Forge` ABC, the concrete API endpoints used by
each adapter:

| # | Method | GitHub endpoint(s) | GitLab endpoint(s) |
|---|--------|-------------------|-------------------|
| 1 | `open_merge_request` | `POST /repos/{o}/{r}/pulls` (422 → `GET` by head) | `POST /projects/:id/merge_requests` (409 → `_find_mr`) |
| 2 | `pr_status` | `GET /repos/{o}/{r}/pulls?head={o}:{h}&state=all` → `GET …/pulls/{num}` | `GET /projects/:id/merge_requests?source_branch=…&state=all&per_page=1` |
| 3 | `pr_status_by_url` | `GET /repos/{o}/{r}/pulls/{number}` | `GET /projects/:id/merge_requests/:iid` |
| 4 | `check_status` | `GET /repos/…/commits/{sha}/check-runs` + `GET …/commits/{sha}/status` + `GET …/check-runs/{id}` | `GET /projects/:id/merge_requests/:iid/pipelines?per_page=1` → `GET …/pipelines/{id}/jobs?scope=failed&per_page=20` |
| 5 | `pr_files` | `GET /repos/{o}/{r}/pulls/{num}/files?per_page=100` | `GET /projects/:id/merge_requests/:iid/changes` |
| 6 | `pr_review_status` | `GET …/pulls/{num}/reviews` + `GET …/pulls/{num}/comments` + `_pr_files` | `GET …/merge_requests/:iid/approvals` + `GET …/merge_requests/:iid/notes` |
| 7 | `merge_pr` | `PUT /repos/{o}/{r}/pulls/{num}/merge` (squash) | `PUT /projects/:id/merge_requests/:iid/merge` (MWPS + squash) |
| 8 | `list_pr_reviews` | `GET /repos/{o}/{r}/pulls/{num}/reviews?per_page=100` | `GET /projects/:id/merge_requests/:iid/notes?per_page=100` (system=false, no position) |
| 9 | `list_review_comments` | `GET /repos/{o}/{r}/pulls/{num}/comments?per_page=100` | `GET /projects/:id/merge_requests/:iid/notes?per_page=100` (with position) |
| 10 | `list_workflow_runs` | `GET /repos/{o}/{r}/actions/runs?status=completed&per_page=30` | `GET /projects/:id/pipelines?per_page=30` |
| 11 | `fetch_workflow_job_logs` | `GET …/actions/runs/{id}/jobs?status=completed` → `GET …/actions/jobs/{id}/logs` | `GET …/pipelines/{id}/jobs?scope=failed&per_page=20` → `GET …/jobs/{id}/trace` |
| 12 | `create_repo` | `POST /orgs/{owner}/repos` → fallback `POST /user/repos` | `POST /projects` (+ `GET /namespaces/:encoded` for namespace resolution) |

Concrete methods:

| Method | GitHub | GitLab |
|--------|--------|--------|
| `update_branch` | `PUT /repos/{o}/{r}/pulls/{number}/update-branch` | `PUT /projects/:id/merge_requests/:iid/rebase` |
| `list_code_scanning_alerts` | `GET /repos/{o}/{r}/code-scanning/alerts?ref=refs/heads/{branch}&state=open` | no-op (`[]`) |
| `delete_branch` | `DELETE /repos/{o}/{r}/git/refs/heads/{branch}` | `DELETE /projects/:id/repository/branches/:branch` |
| `list_branches` | `GET /repos/{o}/{r}/branches?per_page=100` (paginated) | `GET /projects/:id/repository/branches?per_page=100` (paginated) |
| `list_open_pr_branches` | `GET /repos/{o}/{r}/pulls?state=open&per_page=100` (paginated) | `GET /projects/:id/merge_requests?state=opened&per_page=100` (paginated) |

---

## 8. Extension points

To add a third forge provider (e.g. Bitbucket, Gitea):

1. **Subclass `Forge`** in a new module under `src/robotsix_mill/forge/`
   (e.g. `bitbucket.py`).

2. **Implement all 12 abstract methods** with the forge's API semantics.

3. **Optionally override** the 4 concrete methods (`list_code_scanning_alerts`,
   `delete_branch`, `list_branches`, `list_open_pr_branches`) when the
   forge supports those capabilities.

4. **Add a new literal** to the `Settings.forge_kind` type union in
   `src/robotsix_mill/config.py`:
   ```python
   forge_kind: Literal["github", "gitlab", "bitbucket", "none"] = ...
   ```

5. **Register the adapter** in `get_forge()` in `src/robotsix_mill/forge/base.py`:
   ```python
   if kind == "bitbucket":
       from .bitbucket import BitbucketForge
       return BitbucketForge(settings, repo_config=repo_config)
   ```

6. **Follow the HTTP seam pattern** — each API call should live in a
   private `_http_*` method so tests can monkeypatch it away.

7. **Add auth** — the new adapter may need its own auth module or
   leverage the existing `forge_token` path in `Secrets`.  If the
   forge uses a different header scheme, define its own `_build_headers()`.

No other files need to change — the deliver, merge, and CI-monitor
stages all consume the `Forge` ABC through the `get_forge()` factory
and are oblivious to the concrete adapter.
