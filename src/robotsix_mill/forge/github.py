"""GitHub forge adapter — open a Pull Request for an already-pushed
branch via the GitHub REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx

from ._http import _ApiClient
from .base import BranchInfo, Forge, NotConfiguredError, RepoInfo

# Regex for stripping ANSI escape sequences (CSI / SGR).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Maximum number of failed jobs whose logs are fetched per run.
_MAX_FAILED_JOBS = 10

# Earliest-failure markers in a GitHub Actions job log. In an
# ``if: always()`` cascade the step that REALLY failed errors FIRST; later
# steps (gated on always()) re-error with misleading input near the tail. So
# a plain tail-cap of the job log shows only the masking error. We instead
# anchor the captured window on the EARLIEST of these markers.
_LOG_FAILURE_RE = re.compile(
    r"(?:##\[error\]|^[^\n]*\bFATAL\b|\bError:|exit code [1-9]|"
    r"Process completed with exit code [1-9])",
    re.MULTILINE,
)
# When anchoring, keep a little of the log AFTER the first error and spend the
# rest of the budget on the lead-up (where the real error message lives).
_LOG_FAILURE_TAIL_CONTEXT = 4096


def _capture_failure_window(clean_log: str, max_bytes: int) -> str:
    """Return at most *max_bytes* of *clean_log*, centred on the FIRST failure
    marker so an ``if: always()`` cascade can't mask the real failing step.

    If the log fits, it's returned whole. If no failure marker is found (or it
    already falls inside the tail window), this degrades to the historical
    tail-cap (keep the last *max_bytes*).
    """
    if len(clean_log) <= max_bytes:
        return clean_log
    m = _LOG_FAILURE_RE.search(clean_log)
    if m is None or m.start() >= len(clean_log) - max_bytes:
        # No marker, or the first marker is already within the tail window →
        # the tail-cap already shows it.
        return clean_log[-max_bytes:]
    # Anchor: spend most of the budget on the lead-up to the first marker
    # (where the real error message lives), keeping a little after it. Cap the
    # after-context at half the budget so a marker near the log start still
    # keeps its preceding lines.
    tail_after = min(_LOG_FAILURE_TAIL_CONTEXT, max_bytes // 2)
    start = max(0, m.start() - (max_bytes - tail_after))
    end = min(len(clean_log), start + max_bytes)
    prefix = "[log truncated — window anchored on first failure marker]\n"
    return prefix + clean_log[start:end]


_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

# Check-run conclusions that are not "success"-like.
_FAILING_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "action_required",
        "cancelled",
        "startup_failure",
        "stale",
    }
)

# Statuses that mean the check is still in-flight.
_PENDING_STATUSES = frozenset(
    {
        "in_progress",
        "queued",
        "waiting",
        "requested",
        "pending",
    }
)


def _parse_iso_utc(value: str | None) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Accepts a trailing ``Z`` (GitHub's UTC marker). Naive timestamps are
    assumed UTC; aware ones are converted to UTC. Returns the Unix epoch
    (UTC) when *value* is missing or unparseable.
    """
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_owner_repo(remote_url: str) -> tuple[str, str]:
    m = _REMOTE_RE.search(remote_url or "")
    if not m:
        raise RuntimeError(f"cannot parse owner/repo from {remote_url!r}")
    return m.group("owner"), m.group("repo")


def _parse_pr_detail(pr: dict) -> dict:
    """Normalize a GitHub PR detail dict into the standard status shape
    (the same dict ``_get_pr`` / ``pr_status`` return).

    GitHub computes mergeable asynchronously after every force-push.
    Until the computation finishes, mergeable_state is "unknown" and
    ``mergeable`` carries the STALE pre-push value — which the merge
    stage previously treated as a real conflict and bounced into
    REBASING. Surface "still computing" as ``None`` so the caller
    waits the next poll instead.
    """
    mergeable_state = pr.get("mergeable_state")
    mergeable = pr.get("mergeable")
    if mergeable_state in (None, "unknown"):
        mergeable = None
    return {
        "merged": bool(pr.get("merged")),
        "state": pr.get("state", "open"),
        "url": pr.get("html_url", ""),
        "mergeable": mergeable,  # True/False/None
        "mergeable_state": mergeable_state,
        "sha": (pr.get("head") or {}).get("sha", ""),
        "number": pr["number"],
    }


# GitHub's hard cap on a repository description (POST /user|orgs/.../repos).
_MAX_REPO_DESCRIPTION = 350


def _clamp_repo_description(description: str) -> str:
    """Clamp *description* to GitHub's 350-char repo-description limit.

    A longer value yields a 422 ``description cannot be more than 350
    characters``. When truncating, end with an ellipsis so the cut is
    visible; collapse newlines (GitHub stores descriptions single-line).
    """
    text = " ".join((description or "").split())
    if len(text) <= _MAX_REPO_DESCRIPTION:
        return text
    return text[: _MAX_REPO_DESCRIPTION - 1].rstrip() + "…"


def _parse_repo_info(r: dict) -> RepoInfo:
    """Extract ``RepoInfo`` from a GitHub ``POST /repos`` response dict."""
    return RepoInfo(
        id=r["id"],
        name=r["name"],
        clone_url=r["clone_url"],
        html_url=r["html_url"],
    )


class GitHubForge(Forge):
    """GitHub adapter — opens PRs, queries checks/reviews/files, merges, and fetches workflow logs via the GitHub REST API."""

    def __init__(self, settings, repo_config=None):
        super().__init__(settings)
        self._repo_config = repo_config
        from .auth import github_token  # lazy: avoid import cycle with auth.py

        self._http = _ApiClient(
            settings,
            repo_config,
            "github_api_url",
            lambda s, rc: _build_headers(github_token(s, repo_config=rc)),
        )

    @property
    def _remote_url(self) -> str:
        """Effective remote URL: per-repo override, else global setting."""
        if self._repo_config is not None:
            remote = getattr(self._repo_config, "forge_remote_url", None)
            if remote:
                return remote
        return self.settings.forge_remote_url or ""

    @property
    def _owner_repo(self) -> tuple[str, str]:
        return _parse_owner_repo(self._remote_url)

    def open_merge_request(self, *, source_branch: str, title: str, body: str) -> str:
        s = self.settings
        owner, repo = self._owner_repo
        return self._create_pr(
            owner=owner,
            repo=repo,
            head=source_branch,
            base=s.forge_target_branch,
            title=title,
            body=body,
        )

    # --- HTTP seam (monkeypatched in tests) ---
    def _create_pr(
        self,
        *,
        owner: str,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        import time

        payload = {"title": title, "head": head, "base": base, "body": body}
        # GitHub sometimes takes a few seconds to index a freshly-
        # pushed ref before the pulls API can resolve it — the
        # symptom is a 422 with field=head, code=invalid even
        # though the branch is visible via git/refs. Retry the
        # create call a few times before giving up; existing-PR
        # detection runs each round so we don't double-open.
        with self._http.client() as (c, api, headers):
            url = f"{api}/repos/{owner}/{repo}/pulls"
            for attempt in range(4):
                r = c.post(url, headers=headers, json=payload)
                if r.status_code == 201:
                    return r.json()["html_url"]
                # 422 — either "already exists" or a transient
                # post-push indexing race.
                if r.status_code == 422:
                    q = c.get(
                        url,
                        headers=headers,
                        params={"head": f"{owner}:{head}", "state": "open"},
                    )
                    items = q.json() if q.status_code == 200 else []
                    if items:
                        return items[0]["html_url"]
                    # No existing PR; treat as a transient "head
                    # invalid" race when the error body says so,
                    # back off and retry. Final attempt falls
                    # through to RuntimeError below.
                    err_text = r.text or ""
                    if (
                        attempt < 3
                        and '"field":"head"' in err_text
                        and '"code":"invalid"' in err_text
                    ):
                        time.sleep(2**attempt)  # 1s, 2s, 4s
                        continue
                # Non-422 (or final attempt) — surface the error.
                break
            raise RuntimeError(
                f"GitHub PR create failed: {r.status_code} {r.text[:300]}"
            )

    # --- HTTP seam (monkeypatched in tests) ---
    def _create_repo(
        self,
        *,
        name: str,
        owner: str,
        private: bool,
        description: str,
    ) -> RepoInfo:
        from ..config import get_secrets

        from .auth import github_token  # lazy: avoid import cycle

        s = self.settings
        # Repo creation needs a token that can create repos. GitHub App
        # installation tokens cannot create repositories under a personal
        # account, so prefer a dedicated repo-creation PAT when configured;
        # fall back to the normal (App or token) auth otherwise.
        token = get_secrets().forge_repo_create_token or github_token(
            s, repo_config=self._repo_config
        )
        custom_headers = _build_headers(token)
        payload = {
            "name": name,
            "private": private,
            # GitHub rejects repo descriptions over 350 chars with a 422;
            # the meta-agent's draft body easily exceeds that, so clamp.
            "description": _clamp_repo_description(description),
            "auto_init": False,
        }

        with self._http.client() as (c, api, _headers):
            # Primary: create under org
            org_url = f"{api}/orgs/{owner}/repos"
            r = c.post(org_url, headers=custom_headers, json=payload)
            if r.status_code == 201:
                return _parse_repo_info(r.json())
            # Fallback on 403/404: create under user
            if r.status_code in (403, 404):
                user_url = f"{api}/user/repos"
                r2 = c.post(user_url, headers=custom_headers, json=payload)
                if r2.status_code == 201:
                    return _parse_repo_info(r2.json())
                # If the fallback also fails, surface that error instead
                r = r2
            # 422 handling — no retry; repo creation races don't apply
            if r.status_code == 422:
                err_text = r.text or ""
                if "name already exists" in err_text.lower():
                    # Re-run safety: a prior scaffold attempt may have created
                    # the repo before failing later (e.g. on the initial
                    # push). If the existing repo is EMPTY (no commits), reuse
                    # it so the scaffold's force-push completes the job; only a
                    # repo with real content is treated as a genuine conflict.
                    existing = self._reuse_if_empty(
                        c, api, custom_headers, owner, name
                    )
                    if existing is not None:
                        return existing
                    raise RuntimeError(
                        f"Repository '{name}' already exists under '{owner}' "
                        f"and is not empty — refusing to overwrite"
                    )
                raise RuntimeError(
                    f"GitHub repo create failed: {r.status_code} {r.text[:300]}"
                )
            if (
                r.status_code == 403
                and "not accessible by integration" in (r.text or "").lower()
            ):
                raise RuntimeError(
                    "GitHub repo create failed: 403 Resource not accessible by "
                    "integration. A GitHub App installation token cannot create "
                    "repositories under a personal account — set "
                    "`forge_repo_create_token` in secrets to a PAT with "
                    "repo-creation rights (classic: `repo` scope; fine-grained: "
                    "Administration:Read and write on the target account)."
                )
            raise RuntimeError(
                f"GitHub repo create failed: {r.status_code} {r.text[:300]}"
            )

    def _reuse_if_empty(self, c, api, headers, owner, name) -> RepoInfo | None:
        """Return the existing repo's ``RepoInfo`` iff it exists and is EMPTY
        (no commits), else ``None``.

        Used to make repo creation re-run-safe: GitHub's commits endpoint
        returns 409 (``Git Repository is empty``) for a repo with no commits.
        An empty repo is safe for the scaffold to force-push into; a repo with
        real content is not, so we signal a genuine conflict by returning None.

        When *owner* is empty the create fell back to ``/user/repos`` (the
        authenticated user), so resolve that login via ``/user`` — otherwise
        the ``/repos//{name}`` lookup 404s and a re-run wrongly blocks.
        """
        if not owner:
            u = c.get(f"{api}/user", headers=headers)
            owner = u.json().get("login", "") if u.status_code == 200 else ""
            if not owner:
                return None
        repo_url = f"{api}/repos/{owner}/{name}"
        rr = c.get(repo_url, headers=headers)
        if rr.status_code != 200:
            return None
        commits = c.get(f"{repo_url}/commits", headers=headers, params={"per_page": 1})
        # 409 = "Git Repository is empty"; an empty 200 list is empty too.
        is_empty = commits.status_code == 409 or (
            commits.status_code == 200 and not commits.json()
        )
        return _parse_repo_info(rr.json()) if is_empty else None

    # --- HTTP seam (monkeypatched in tests) ---
    def _fork_repo(
        self,
        *,
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> RepoInfo:
        from ..config import get_secrets

        from .auth import github_token  # lazy: avoid import cycle

        s = self.settings
        token = get_secrets().forge_repo_create_token or github_token(
            s, repo_config=self._repo_config
        )
        custom_headers = _build_headers(token)
        url = f"/repos/{source_owner}/{source_repo}/forks"
        payload: dict = {}
        if target_namespace is not None:
            payload["organization"] = target_namespace

        with self._http.client() as (c, api, _headers):
            r = c.post(f"{api}{url}", headers=custom_headers, json=payload)
            if r.status_code in (200, 201, 202):
                return _parse_repo_info(r.json())
            if (
                r.status_code == 403
                and "not accessible by integration" in (r.text or "").lower()
            ):
                raise RuntimeError(
                    "GitHub fork failed: 403 Resource not accessible by "
                    "integration. A GitHub App installation token cannot fork "
                    "repositories under a personal account — set "
                    "`forge_repo_create_token` in secrets to a PAT with "
                    "repo-creation rights (classic: `repo` scope; fine-grained: "
                    "Administration:Read and write on the target account)."
                )
            raise RuntimeError(f"GitHub fork failed: {r.status_code} {r.text[:300]}")

    def pr_status(self, *, source_branch: str) -> dict | None:
        owner, repo = self._owner_repo
        return self._get_pr(owner=owner, repo=repo, head=source_branch)

    def pr_status_by_url(self, *, url: str) -> dict | None:
        m = re.search(r"/pull/(\d+)", url or "")
        if not m:
            return None
        owner, repo = self._owner_repo
        return self._get_pr_by_number(owner=owner, repo=repo, number=int(m.group(1)))

    def check_status(self, *, source_branch: str) -> dict | None:
        owner, repo = self._owner_repo
        return self._check_status(owner=owner, repo=repo, head=source_branch)

    def pr_files(self, *, source_branch: str) -> list[dict]:
        owner, repo = self._owner_repo
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return []
        return self._pr_files(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def merge_pr(self, *, source_branch: str) -> dict:
        owner, repo = self._owner_repo
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return {"merged": False, "reason": "PR not found"}
        return self._merge_pr(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def list_pr_reviews(self, *, source_branch: str) -> list[dict]:
        owner, repo = self._owner_repo
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return []
        return self._list_pr_reviews(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def list_review_comments(self, *, source_branch: str) -> list[dict]:
        owner, repo = self._owner_repo
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return []
        return self._list_review_comments(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def pr_review_status(self, *, source_branch: str) -> dict | None:
        owner, repo = self._owner_repo
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return None
        return self._pr_review_status(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict]:
        owner, repo = self._owner_repo
        return self._list_workflow_runs(
            owner=owner,
            repo=repo,
            branch=branch,
            head_sha=head_sha,
        )

    def list_code_scanning_alerts(self, *, source_branch: str) -> list[dict]:
        owner, repo = self._owner_repo
        try:
            r = self._http.get(
                f"/repos/{owner}/{repo}/code-scanning/alerts",
                params={
                    "ref": f"refs/heads/{source_branch}",
                    "state": "open",
                    "per_page": 50,
                },
            )
            # 404 = code-scanning not enabled / no alerts endpoint; 403 =
            # token lacks the security-events scope. Either way: no signal,
            # not an error — degrade to "no alerts".
            if r.status_code in (403, 404):
                return []
            r.raise_for_status()
            raw = r.json()
        except Exception:  # noqa: BLE001 — best-effort enrichment, never fatal
            return []
        out: list[dict] = []
        for a in raw if isinstance(raw, list) else []:
            rule = a.get("rule") or {}
            inst = a.get("most_recent_instance") or {}
            loc = inst.get("location") or {}
            out.append(
                {
                    "rule": rule.get("id", ""),
                    "severity": rule.get("security_severity_level")
                    or rule.get("severity", ""),
                    "path": loc.get("path", ""),
                    "line": loc.get("start_line"),
                    "message": (inst.get("message") or {}).get("text", "")
                    or rule.get("description", ""),
                    "url": a.get("html_url", ""),
                }
            )
        return out

    def fetch_workflow_job_logs(self, *, run_id: int) -> str:
        owner, repo = self._owner_repo
        return self._fetch_workflow_job_logs(
            owner=owner,
            repo=repo,
            run_id=run_id,
        )

    def create_repo(
        self, *, name: str, owner: str, private: bool, description: str
    ) -> RepoInfo:
        if not self.settings.enable_repo_creation:
            raise NotConfiguredError(
                "Repo creation is disabled. Set enable_repo_creation=True "
                "and verify the GitHub App installation has the "
                "Administration:Read and write permission (or equivalent "
                "PAT scope)."
            )
        return self._create_repo(
            name=name,
            owner=owner,
            private=private,
            description=description,
        )

    def fork_repo(
        self,
        *,
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> RepoInfo:
        if not self.settings.enable_repo_creation:
            raise NotConfiguredError(
                "Repo creation is disabled. Set enable_repo_creation=True "
                "and verify the GitHub App installation has the "
                "Administration:Read and write permission (or equivalent "
                "PAT scope)."
            )
        return self._fork_repo(
            source_owner=source_owner,
            source_repo=source_repo,
            target_namespace=target_namespace,
        )

    def delete_branch(self, *, branch: str) -> bool:
        owner, repo = self._owner_repo
        return self._delete_branch(owner=owner, repo=repo, branch=branch)

    def list_branches(self) -> list[BranchInfo]:
        owner, repo = self._owner_repo
        return self._list_branches(owner=owner, repo=repo)

    def list_open_pr_branches(self) -> set[str]:
        owner, repo = self._owner_repo
        return self._list_open_pr_branches(owner=owner, repo=repo)

    # --- HTTP seamm (monkeypatched in tests) ---
    def _get_pr(self, *, owner: str, repo: str, head: str) -> dict | None:
        with self._http.client() as (c, api, headers):
            lst = c.get(
                f"{api}/repos/{owner}/{repo}/pulls",
                headers=headers,
                params={"head": f"{owner}:{head}", "state": "all"},
            )
            lst.raise_for_status()
            items = lst.json()
            if not items:
                return None
            num = items[0]["number"]
            d = c.get(f"{api}/repos/{owner}/{repo}/pulls/{num}", headers=headers)
            d.raise_for_status()
            pr = d.json()
        return _parse_pr_detail(pr)

    # --- HTTP seam (monkeypatched in tests) ---
    def _get_pr_by_number(self, *, owner: str, repo: str, number: int) -> dict | None:
        """Fetch a PR's status directly by number via a single
        ``GET /repos/{owner}/{repo}/pulls/{number}``.

        Returns the same dict shape as ``_get_pr`` (including the
        ``mergeable_state`` → ``mergeable`` normalization). Used by
        ``pr_status_by_url`` to resolve a recorded PR url even after the
        head branch was auto-deleted on merge (which makes the
        branch-keyed ``_get_pr`` list come back empty)."""
        r = self._http.get(f"/repos/{owner}/{repo}/pulls/{number}")
        r.raise_for_status()
        pr = r.json()
        return _parse_pr_detail(pr)

    # --- HTTP seam (monkeypatched in tests) ---
    def _pr_files(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict]:
        try:
            r = self._http.get(
                f"/repos/{owner}/{repo}/pulls/{pull_number}/files",
                params={"per_page": 100},
            )
            r.raise_for_status()
            items = r.json()
        except Exception:
            return []
        return [
            {
                "path": item["filename"],
                "status": item.get("status", "modified"),
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
            }
            for item in items
        ]

    # --- HTTP seam (monkeypatched in tests) ---
    def _merge_pr(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict:
        try:
            r = self._http.put(
                f"/repos/{owner}/{repo}/pulls/{pull_number}/merge",
                json={"merge_method": "squash"},
            )
            if r.status_code == 200:
                return {"merged": True, "reason": "merged"}
            if r.status_code == 405:
                return {
                    "merged": False,
                    "reason": "merge not allowed (branch protection?)",
                }
            if r.status_code == 409:
                return {"merged": False, "reason": "PR is not mergeable"}
            return {
                "merged": False,
                "reason": f"HTTP {r.status_code}: {r.text[:200]}",
            }
        except Exception as e:
            return {"merged": False, "reason": str(e)}

    # --- HTTP seam (monkeypatched in tests) ---
    def _delete_branch(self, *, owner: str, repo: str, branch: str) -> bool:
        try:
            r = self._http.delete(
                f"/repos/{owner}/{repo}/git/refs/heads/{branch}"
            )
            # 204 = deleted; 404/422 = ref does not exist (already gone,
            # e.g. by GitHub auto-delete) — the branch is gone either way,
            # which is the desired end state.
            if r.status_code in (204, 404, 422):
                return True
            return False
        except Exception:
            return False

    # --- HTTP seam (monkeypatched in tests) ---
    def _list_branches(self, *, owner: str, repo: str) -> list[BranchInfo]:
        out: list[BranchInfo] = []
        try:
            with self._http.client() as (c, api, headers):
                url = f"{api}/repos/{owner}/{repo}/branches"
                page = 1
                while True:
                    r = c.get(
                        url,
                        headers=headers,
                        params={"per_page": 100, "page": page},
                    )
                    r.raise_for_status()
                    items = r.json()
                    for b in items:
                        date = (
                            ((b.get("commit") or {}).get("commit") or {}).get(
                                "committer"
                            )
                            or {}
                        ).get("date")
                        out.append(
                            BranchInfo(
                                name=b["name"],
                                last_commit_at=_parse_iso_utc(date),
                                is_protected=bool(b.get("protected")),
                            )
                        )
                    if len(items) < 100:
                        break
                    page += 1
        except Exception:
            return []
        return out

    # --- HTTP seam (monkeypatched in tests) ---
    def _list_open_pr_branches(self, *, owner: str, repo: str) -> set[str]:
        out: set[str] = set()
        try:
            with self._http.client() as (c, api, headers):
                url = f"{api}/repos/{owner}/{repo}/pulls"
                page = 1
                while True:
                    r = c.get(
                        url,
                        headers=headers,
                        params={"state": "open", "per_page": 100, "page": page},
                    )
                    r.raise_for_status()
                    items = r.json()
                    for pr in items:
                        ref = (pr.get("head") or {}).get("ref")
                        if ref:
                            out.add(ref)
                    if len(items) < 100:
                        break
                    page += 1
        except Exception:
            return set()
        return out

    # --- HTTP seam (monkeypatched in tests) ---
    def _list_pr_reviews(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict]:
        r = self._http.get(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            params={"per_page": 100},
        )
        r.raise_for_status()
        items = r.json()
        return [
            {
                "id": item["id"],
                "author": (item.get("user") or {}).get("login", ""),
                "created_at": item.get("submitted_at", ""),
                "body": item.get("body") or "",
            }
            for item in items
        ]

    # --- HTTP seam (monkeypatched in tests) ---
    def _list_review_comments(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict]:
        r = self._http.get(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/comments",
            params={"per_page": 100},
        )
        r.raise_for_status()
        items = r.json()
        return [
            {
                "id": item["id"],
                "author": (item.get("user") or {}).get("login", ""),
                "created_at": item.get("created_at", ""),
                "body": item.get("body") or "",
                "file_path": item.get("path", ""),
                "line": item.get("line") or item.get("original_line"),
                "diff_hunk": item.get("diff_hunk", ""),
            }
            for item in items
        ]

    # --- HTTP seam (monkeypatched in tests) ---
    def _pr_review_status(
        self,
        *,
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict:
        with self._http.client() as (c, api, headers):
            # 1. Fetch reviews (includes state field that list_pr_reviews drops).
            r = c.get(
                f"{api}/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
                headers=headers,
                params={"per_page": 100},
            )
            r.raise_for_status()
            reviews_raw = r.json()

            # 2. Fetch inline review comments.
            r2 = c.get(
                f"{api}/repos/{owner}/{repo}/pulls/{pull_number}/comments",
                headers=headers,
                params={"per_page": 100},
            )
            r2.raise_for_status()
            comments_raw = r2.json()

            # 3. Fetch changed files.
            files = self._pr_files(
                owner=owner,
                repo=repo,
                pull_number=pull_number,
            )

        # Determine aggregate review state from the latest non-dismissed
        # review.  GitHub returns reviews oldest-first; iterate reversed.
        state = "PENDING"
        for rev in reversed(reviews_raw):
            rev_state = rev.get("state", "COMMENTED")
            if rev_state != "DISMISSED":
                state = rev_state
                break
        else:
            # All reviews are DISMISSED — use the latest one.
            if reviews_raw:
                state = reviews_raw[-1].get("state", "DISMISSED")

        # Build a review_state lookup: review_id -> state.
        review_state_map: dict[int, str] = {}
        for rev in reviews_raw:
            review_state_map[rev["id"]] = rev.get("state", "COMMENTED")

        # Merge review body comments + inline comments into one list.
        comments: list[dict] = []
        for rev in reviews_raw:
            body = rev.get("body")
            if body and body.strip():
                comments.append(
                    {
                        "body": body,
                        "path": "",
                        "line": None,
                        "review_state": rev.get("state", "COMMENTED"),
                    }
                )
        for c in comments_raw:
            comments.append(
                {
                    "body": c.get("body") or "",
                    "path": c.get("path", ""),
                    "line": c.get("line") or c.get("original_line"),
                    "review_state": review_state_map.get(
                        c.get("pull_request_review_id"), "COMMENTED"
                    ),
                }
            )

        return {
            "state": state,
            "comments": comments,
            "files": [f["path"] for f in files],
        }

    # --- HTTP seam (monkeypatched in tests) ---
    def _check_status(self, *, owner: str, repo: str, head: str) -> dict | None:
        pr = self._get_pr(owner=owner, repo=repo, head=head)
        if pr is None:
            return None

        sha = pr.get("sha", "")
        if not sha:
            return None

        with self._http.client() as (c, api, headers):
            # 1. Fetch check runs (any status — completed, in_progress,
            # queued — so a brand-new SHA with a workflow that's been
            # queued but not started is correctly classified "pending"
            # rather than "no CI configured" below.
            #
            # A 403 here means the App installation lacks ``checks: read``
            # for this repo. That's a config gap, not a transient error
            # — treat it as "no check_runs visible" and fall through to
            # statuses + no-CI handling.
            check_runs: list[dict] = []
            cr_resp = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/check-runs",
                headers=headers,
                params={"per_page": 100},
            )
            if cr_resp.status_code != 403:
                cr_resp.raise_for_status()
                check_runs = cr_resp.json().get("check_runs", [])

            # 2. Always probe combined statuses too. A repo without
            # any CI returns empty check_runs AND empty
            # statuses_data["statuses"] — we use that to distinguish
            # "no CI configured" (pass-through) from "CI pending"
            # (wait). 403 on statuses follows the same logic.
            status_runs: list[dict] = []
            st_resp = c.get(
                f"{api}/repos/{owner}/{repo}/commits/{sha}/status",
                headers=headers,
            )
            if st_resp.status_code != 403:
                st_resp.raise_for_status()
                statuses_data = st_resp.json()
                status_runs = _statuses_to_check_runs(statuses_data)
            if not check_runs:
                check_runs = status_runs

            # No checks AND no statuses (either truly empty or the
            # App lacks read permission for both endpoints) → there
            # is nothing meaningful to gate on. Treat as success so
            # the merge stage doesn't loop forever.
            if not check_runs and not status_runs:
                return {"conclusion": "success", "failing": []}

            return _derive_check_conclusion(c, api, owner, repo, headers, check_runs)

    # --- HTTP seam (monkeypatched in tests) ---
    def _list_workflow_runs(
        self,
        *,
        owner: str,
        repo: str,
        branch: str | None,
        head_sha: str | None,
    ) -> list[dict]:
        params: dict = {"status": "completed", "per_page": 30}
        if branch is not None:
            params["branch"] = branch
        if head_sha is not None:
            params["head_sha"] = head_sha

        r = self._http.get(
            f"/repos/{owner}/{repo}/actions/runs",
            params=params,
        )
        r.raise_for_status()
        raw = r.json().get("workflow_runs", [])
        return [
            {
                "id": run["id"],
                "name": run.get("name", ""),
                "workflow_id": run.get("workflow_id"),
                "head_sha": run.get("head_sha", ""),
                "conclusion": run.get("conclusion"),
                "html_url": run.get("html_url", ""),
                "created_at": run.get("created_at", ""),
            }
            for run in raw
        ]

    # --- HTTP seam (monkeypatched in tests) ---
    def _fetch_workflow_job_logs(
        self,
        *,
        owner: str,
        repo: str,
        run_id: int,
    ) -> str:
        s = self.settings

        with self._http.client() as (c, api, headers):
            # 1. List jobs for the run.
            jobs_resp = c.get(
                f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                headers=headers,
                params={"status": "completed"},
            )
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json().get("jobs", [])

        # 2. Filter to failed-like jobs.
        failed_conclusions = frozenset(
            {
                "failure",
                "cancelled",
                "timed_out",
                "action_required",
            }
        )
        failed_jobs = [j for j in jobs if j.get("conclusion") in failed_conclusions][
            :_MAX_FAILED_JOBS
        ]

        if not failed_jobs:
            return ""

        parts: list[str] = []
        log_max = s.ci_log_max_bytes

        with self._http.client() as (c, api, headers):
            for j in failed_jobs:
                job_id = j["id"]
                job_name = j.get("name", f"job-{job_id}")
                try:
                    log_resp = c.get(
                        f"{api}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                        headers=headers,
                        follow_redirects=True,
                    )
                    log_resp.raise_for_status()
                    raw = log_resp.text
                except httpx.HTTPStatusError:
                    sc = log_resp.status_code
                    if sc == 403:
                        raw = f"[log fetch failed for job {job_id}: HTTP 403 — App likely missing Actions:Read permission]"
                    else:
                        raw = f"[log fetch failed for job {job_id}: HTTP {sc}]"
                except Exception as exc:
                    raw = f"[log fetch failed for job {job_id}: {type(exc).__name__}]"
                else:
                    if not raw:
                        raw = f"[log fetch returned empty body for job {job_id}]"

                # Strip ANSI.
                clean = _ANSI_RE.sub("", raw)
                # Capture the window around the FIRST failure marker (not a
                # blind tail-cap) so an ``if: always()`` cascade — where a
                # downstream always-step re-errors with misleading input —
                # can't mask the step that actually failed first.
                clean = _capture_failure_window(clean, log_max)

                parts.append(f"### Job: {job_name} (id={job_id})\n")
                parts.append(clean)
                parts.append("\n")

        return "\n".join(parts)


def _statuses_to_check_runs(statuses_data: dict) -> list[dict]:
    """Convert combined statuses response into check-run–shaped dicts."""
    statuses = statuses_data.get("statuses", [])
    if not statuses:
        return []
    # Collapse per-context into a single item.
    by_context: dict[str, list[dict]] = {}
    for st in statuses:
        ctx = st.get("context", "")
        by_context.setdefault(ctx, []).append(st)
    runs = []
    for ctx, items in by_context.items():
        # overall state: "success", "failure", "pending"
        state = statuses_data.get("state", "success")
        conclusion = state if state != "pending" else None
        runs.append(
            {
                "id": None,  # no detail fetch for statuses
                "name": ctx,
                "status": "completed" if state != "pending" else "in_progress",
                "conclusion": conclusion,
                "output": {
                    "summary": None,
                    "text": None,
                    "annotations": [],
                },
            }
        )
    return runs


def _conclusion_for_check(cr: dict) -> str:
    """Classify a single check run as 'pending', 'failure', or 'neutral'."""
    if cr.get("status", "") in _PENDING_STATUSES:
        return "pending"
    if cr.get("conclusion") in _FAILING_CONCLUSIONS:
        return "failure"
    return "neutral"


def _extract_annotations(
    client,
    api: str,
    owner: str,
    repo: str,
    headers: dict,
    cr: dict,
) -> dict:
    """Fetch and parse annotations for a failing check run (best-effort)."""
    cr_id = cr.get("id")
    name = cr.get("name", "unknown")
    summary = None
    text = None
    annotations: list[dict] = []

    if cr_id is not None:
        try:
            detail = client.get(
                f"{api}/repos/{owner}/{repo}/check-runs/{cr_id}",
                headers=headers,
            )
            detail.raise_for_status()
            output = detail.json().get("output", {}) or {}
            summary = output.get("summary")
            text = output.get("text")
            raw_anns = output.get("annotations") or []
            annotations = [
                {
                    "path": a.get("path", ""),
                    "start_line": a.get("start_line"),
                    "message": a.get("message", ""),
                    "level": a.get("annotation_level", "failure"),
                }
                for a in raw_anns[:20]
            ]
        except Exception:
            pass  # detail fetch is best-effort

    # Apply truncation.
    if summary and len(summary) > 2000:
        summary = summary[:1999] + "…"
    if text and len(text) > 4000:
        text = text[:3999] + "…"

    return {
        "name": name,
        "summary": summary,
        "text": text,
        "annotations": annotations,
    }


def _derive_check_conclusion(
    client,
    api: str,
    owner: str,
    repo: str,
    headers: dict,
    check_runs: list[dict],
) -> dict:
    """Derive the overall conclusion and build the failing list."""
    if not check_runs:
        return {"conclusion": None, "failing": []}

    has_pending = False
    has_failure = False
    failing: list[dict] = []

    for cr in check_runs:
        cat = _conclusion_for_check(cr)
        if cat == "pending":
            has_pending = True
        elif cat == "failure":
            has_failure = True
            failing.append(_extract_annotations(client, api, owner, repo, headers, cr))

    if has_failure:
        return {"conclusion": "failure", "failing": failing}
    if has_pending:
        return {"conclusion": "pending", "failing": []}
    return {"conclusion": "success", "failing": []}
