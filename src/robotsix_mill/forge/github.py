"""GitHub forge adapter — open a Pull Request for an already-pushed
branch via the GitHub REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import httpx

from ._http import _ApiClient
from ._log_utils import _capture_failure_window
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


_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

# Check-run conclusions that are genuine, terminal failures.
_FAILING_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "action_required",
        "startup_failure",
    }
)

# Inconclusive conclusions: the check produced NO verdict because a newer
# run superseded it (GitHub Actions ``concurrency: cancel-in-progress``
# marks the old run ``cancelled``; ``stale`` is the equivalent for status
# checks). Treating these as failures turned routine concurrency churn
# into false CI failures — which spawned ci_fix tickets whose pushes
# cancelled yet more runs, a self-sustaining loop. Classify them as
# PENDING instead so the merge gate waits for a real verdict; once the
# false failures stop, the last (uncancelled) run completes and resolves.
_INCONCLUSIVE_CONCLUSIONS = frozenset({"cancelled", "stale"})

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
        from .auth import (  # lazy: avoid import cycle with auth.py
            github_token,
            invalidate_github_token,
        )

        self._http = _ApiClient(
            settings,
            repo_config,
            "github_api_url",
            lambda s, rc: _build_headers(github_token(s, repo_config=rc)),
        )
        if settings.forge_auth == "app":
            self._http._on_401 = lambda: invalidate_github_token(
                self.settings, self._repo_config
            )

    @property
    def _remote_url(self) -> str:
        """Effective remote URL: per-repo override, else global setting.

        For a repo with a ``cross_repo_target`` the effective remote is
        the *upstream* repo PRs are opened against — so PR create,
        status polling and merge all naturally target upstream. The
        push (to the fork) is driven separately by the deliver stage."""
        if self._repo_config is not None:
            cct = getattr(self._repo_config, "cross_repo_target", None)
            if cct is not None and cct.upstream_remote_url:
                return cct.upstream_remote_url
            remote = getattr(self._repo_config, "forge_remote_url", None)
            if remote:
                return remote
        return self.settings.forge_remote_url or ""

    @property
    def _owner_repo(self) -> tuple[str, str]:
        return _parse_owner_repo(self._remote_url)

    @property
    def _head_owner(self) -> str:
        """Owner of the head branch for PR lookups.

        For cross-repo targets the head lives on the fork, so this
        returns the fork owner; otherwise it's the same as the
        upstream/remote owner.  Used by ``_get_pr`` to build the
        ``head=<owner>:<branch>`` filter so the lookup finds the PR
        whose head branch is owned by the fork, not the upstream.
        """
        if self._repo_config is not None:
            cct = getattr(self._repo_config, "cross_repo_target", None)
            if cct is not None and cct.fork_remote_url:
                fork_owner, _ = _parse_owner_repo(cct.fork_remote_url)
                return fork_owner
        return self._owner_repo[0]

    def open_merge_request(
        self,
        *,
        source_branch: str,
        title: str,
        body: str,
        head_repo: str | None = None,
    ) -> str:
        """Open a Pull Request for the already-pushed *source_branch*.

        :param source_branch: head branch to open the PR from.
        :param title: PR title.
        :param body: PR description body.
        :param head_repo: when set (``owner/repo``), a cross-fork PR whose
            head lives on the fork; the head is qualified ``owner:branch``
            and the base resolves to the upstream ``base_branch``.
        Returns the new (or already-existing) PR's ``html_url``. Calls the
        GitHub API to create the PR (idempotent: reuses an open PR for the
        same head instead of double-opening). Raises ``RuntimeError`` on a
        non-recoverable create failure.
        """
        s = self.settings
        owner, repo = self._owner_repo
        from ..config import target_branch_for  # lazy: avoid import cycle

        base = target_branch_for(s, self._repo_config)
        head = source_branch
        if head_repo is not None:
            # Cross-fork PR: head lives in the fork (``owner:branch``),
            # base on the upstream repo / ``base_branch``. ``_owner_repo``
            # already resolves to upstream via ``cross_repo_target``.
            fork_owner = head_repo.split("/", 1)[0]
            head = f"{fork_owner}:{source_branch}"
            cct = getattr(self._repo_config, "cross_repo_target", None)
            if cct is not None:
                base = cct.base_branch
        return self._create_pr(
            owner=owner,
            repo=repo,
            head=head,
            base=base,
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

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        payload = {"title": title, "head": head, "base": base, "body": body}
        # GitHub sometimes takes a few seconds to index a freshly-
        # pushed ref before the pulls API can resolve it — the
        # symptom is a 422 with field=head, code=invalid even
        # though the branch is visible via git/refs. Retry the
        # create call a few times before giving up; existing-PR
        # detection runs each round so we don't double-open.
        with self._http.client() as (c, api, headers):
            url = f"{api}/repos/{owner}/{repo}/pulls"
            already_retried_401 = False
            for attempt in range(4):
                r = c.post(url, headers=headers, json=payload)
                if r.status_code == 201:
                    return r.json()["html_url"]
                # 422 — either "already exists" or a transient
                # post-push indexing race.
                if r.status_code == 422:
                    # head is already fully qualified for cross-fork
                    # PRs (e.g. "fork-owner:branch"); for same-repo
                    # PRs it's just the branch name and needs the
                    # owner prefix.
                    head_param = head if ":" in head else f"{owner}:{head}"
                    q = c.get(
                        url,
                        headers=headers,
                        params={"head": head_param, "state": "open"},
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
                # 401 — intermittent App-token write auth flap
                # (GitHub replica lag). Invalidate cached token,
                # back off, regenerate headers, retry once.
                if r.status_code == 401 and not already_retried_401:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    headers = self._http.regenerate_headers()
                    already_retried_401 = True
                    continue
                # Non-422 / non-401 (or final attempt) — surface.
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
        private: bool | None = None,
        description: str,
    ) -> RepoInfo:
        import time

        if private is None:
            private = self.settings.repo_visibility_default == "private"

        from ..config import get_secrets

        from .auth import (
            github_token,
            invalidate_github_token,
        )  # lazy: avoid import cycle

        s = self.settings
        # Repo creation needs a token that can create repos. GitHub App
        # installation tokens cannot create repositories under a personal
        # account, so prefer a dedicated repo-creation PAT when configured;
        # fall back to the normal (App or token) auth otherwise.
        token = get_secrets().forge_repo_create_token or github_token(
            s, repo_config=self._repo_config
        )
        custom_headers = _build_headers(token)

        def _create_attempt(c, api):
            # Primary: create under org
            org_url = f"{api}/orgs/{owner}/repos"
            r = c.post(org_url, headers=custom_headers, json=payload)
            if r.status_code == 201:
                return r, False  # response, not_a_401
            if r.status_code in (403, 404):
                user_url = f"{api}/user/repos"
                r2 = c.post(user_url, headers=custom_headers, json=payload)
                if r2.status_code == 201:
                    return r2, False
                r = r2
            if r.status_code == 401:
                return r, True  # signal 401
            return r, False

        payload = {
            "name": name,
            "private": private,
            # GitHub rejects repo descriptions over 350 chars with a 422;
            # the meta-agent's draft body easily exceeds that, so clamp.
            "description": _clamp_repo_description(description),
            "auto_init": False,
        }

        for retry in range(2):
            with self._http.client() as (c, api, _headers):
                r, is_401 = _create_attempt(c, api)
                if is_401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    token = get_secrets().forge_repo_create_token or github_token(
                        s, repo_config=self._repo_config
                    )
                    custom_headers = _build_headers(token)
                    continue
            break  # success or final attempt

        # Post-request error handling (original logic preserved).
        if r.status_code == 201:
            return _parse_repo_info(r.json())
        # 422 handling — no retry; repo creation races don't apply
        if r.status_code == 422:
            err_text = r.text or ""
            if "name already exists" in err_text.lower():
                # Re-run safety: a prior scaffold attempt may have created
                # the repo before failing later (e.g. on the initial
                # push). If the existing repo is EMPTY (no commits), reuse
                # it so the scaffold's force-push completes the job; only a
                # repo with real content is treated as a genuine conflict.
                with self._http.client() as (c, api, _headers):
                    existing = self._reuse_if_empty(c, api, custom_headers, owner, name)
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
        raise RuntimeError(f"GitHub repo create failed: {r.status_code} {r.text[:300]}")

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
        import time

        from ..config import get_secrets

        from .auth import (
            github_token,
            invalidate_github_token,
        )  # lazy: avoid import cycle

        s = self.settings
        token = get_secrets().forge_repo_create_token or github_token(
            s, repo_config=self._repo_config
        )
        custom_headers = _build_headers(token)
        url = f"/repos/{source_owner}/{source_repo}/forks"
        payload: dict = {}
        if target_namespace is not None:
            payload["organization"] = target_namespace

        for retry in range(2):
            with self._http.client() as (c, api, _headers):
                r = c.post(f"{api}{url}", headers=custom_headers, json=payload)
                if r.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    token = get_secrets().forge_repo_create_token or github_token(
                        s, repo_config=self._repo_config
                    )
                    custom_headers = _build_headers(token)
                    continue
            break  # success or final attempt

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
        """Return the PR status for the PR whose head is *source_branch*.

        Looks the PR up by head branch and returns the normalized status
        ``dict`` (``merged``, ``state``, ``url``, ``mergeable``,
        ``mergeable_state``, ``sha``, ``number``), or ``None`` when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo
        return self._get_pr(owner=owner, repo=repo, head=source_branch)

    def pr_status_by_url(self, *, url: str) -> dict | None:
        """Return the PR status resolved directly from a PR *url*.

        Parses the ``/pull/<number>`` segment out of *url* and fetches the
        PR by number, returning the same status ``dict`` shape as
        :meth:`pr_status`. Returns ``None`` when *url* has no PR number.
        Unlike :meth:`pr_status` this still resolves a merged PR whose head
        branch was auto-deleted on merge.
        """
        m = re.search(r"/pull/(\d+)", url or "")
        if not m:
            return None
        owner, repo = self._owner_repo
        return self._get_pr_by_number(owner=owner, repo=repo, number=int(m.group(1)))

    def check_status(self, *, source_branch: str) -> dict | None:
        """Return the aggregate CI check status for *source_branch*'s PR head.

        Returns a ``dict`` with ``conclusion`` (``"success"`` /
        ``"failure"`` / ``"pending"``) and a ``failing`` list of failing-
        check detail dicts, or ``None`` when there is no PR / head SHA to
        gate on. A repo with no CI configured reports ``"success"`` so the
        merge pipeline does not wait forever.
        """
        owner, repo = self._owner_repo
        return self._check_status(owner=owner, repo=repo, head=source_branch)

    def pr_files(self, *, source_branch: str) -> list[dict]:
        """Return the list of files changed in *source_branch*'s PR.

        Each entry is a ``dict`` with ``path``, ``status``, ``additions``,
        and ``deletions``. Returns ``[]`` when no PR exists for the branch.
        """
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
        """Merge (squash) the PR whose head is *source_branch*.

        Returns a ``dict`` with ``merged`` (bool) and a ``reason`` string.
        Mutates remote state: squash-merges the PR via the GitHub API.
        Returns ``{"merged": False, "reason": "PR not found"}`` when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return {"merged": False, "reason": "PR not found"}
        return self._merge_pr(
            owner=owner,
            repo=repo,
            pull_number=pr["number"],
        )

    def update_branch(self, *, source_branch: str) -> dict:
        """Update *source_branch*'s PR head with the latest base branch.

        Calls the GitHub ``update-branch`` API (merges the base into the PR
        head), mutating remote state. Returns a ``dict`` with ``updated``
        (bool) and a ``reason`` string — ``False``/"already up to date" when
        there is nothing to merge, ``False``/"PR not found" when no PR
        exists for the branch.
        """
        owner, repo = self._owner_repo
        pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        if pr is None:
            return {"updated": False, "reason": "PR not found"}
        try:
            r = self._http.put(
                f"/repos/{owner}/{repo}/pulls/{pr['number']}/update-branch"
            )
            if r.status_code == 202:
                return {"updated": True, "reason": "update-branch accepted"}
            if r.status_code == 422:
                # branch already up to date — nothing to do
                return {"updated": False, "reason": "already up to date"}
            return {"updated": False, "reason": f"HTTP {r.status_code}: {r.text[:200]}"}
        except Exception as e:  # noqa: BLE001
            return {"updated": False, "reason": str(e)}

    def list_pr_reviews(self, *, source_branch: str) -> list[dict]:
        """Return the reviews submitted on *source_branch*'s PR.

        Each entry is a ``dict`` with ``id``, ``author``, ``created_at``,
        and ``body``. Returns ``[]`` when no PR exists for the branch.
        """
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
        """Return the inline review comments on *source_branch*'s PR.

        Each entry is a ``dict`` with ``id``, ``author``, ``created_at``,
        ``body``, ``file_path``, ``line``, and ``diff_hunk``. Returns ``[]``
        when no PR exists for the branch.
        """
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
        """Return the aggregate review status for *source_branch*'s PR.

        Returns a ``dict`` with ``state`` (the latest non-dismissed review
        state, e.g. ``"CHANGES_REQUESTED"`` / ``"APPROVED"`` / ``"PENDING"``),
        a ``comments`` list (review bodies + inline comments, each carrying
        its ``review_state``), and ``files`` (changed file paths). Returns
        ``None`` when no PR exists for the branch.
        """
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
        """Return completed GitHub Actions workflow runs.

        :param branch: when set, filter runs to this branch.
        :param head_sha: when set, filter runs to this head commit SHA.
        Returns a ``list[dict]`` (one per run) with ``id``, ``name``,
        ``workflow_id``, ``head_sha``, ``conclusion``, ``html_url``, and
        ``created_at``.
        """
        owner, repo = self._owner_repo
        return self._list_workflow_runs(
            owner=owner,
            repo=repo,
            branch=branch,
            head_sha=head_sha,
        )

    # --- HTTP seam (monkeypatched in tests) ---
    def _fetch_alerts_for_ref(self, *, owner: str, repo: str, ref: str) -> list[dict]:
        """Fetch raw open code-scanning alerts for a single *ref* (best-effort).

        Degrades to ``[]`` on 403/404 (code-scanning off / token lacks the
        security-events scope) or any other error — never fatal.
        """
        try:
            r = self._http.get(
                f"/repos/{owner}/{repo}/code-scanning/alerts",
                params={"ref": ref, "state": "open", "per_page": 50},
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
        return raw if isinstance(raw, list) else []

    def list_code_scanning_alerts(self, *, source_branch: str) -> list[dict]:
        """Return open code-scanning (CodeQL) alerts for *source_branch*.

        Queries both the PR merge ref and the branch ref, unioning the
        results (de-duped on the raw alert number) so both CodeQL workflow
        shapes are covered. Each entry is a ``dict`` with ``rule``,
        ``severity``, ``path``, ``line``, ``message``, and ``url``.
        Best-effort: degrades to ``[]`` when code-scanning is off or the
        token lacks the security-events scope.
        """
        owner, repo = self._owner_repo
        # A CodeQL workflow that only triggers on ``pull_request`` (the common
        # case) files its alerts under the PR merge ref ``refs/pull/{N}/merge``,
        # NOT ``refs/heads/{branch}`` — a feature-branch push never runs that
        # analysis. Resolve the PR for this branch and query BOTH the merge ref
        # and the branch ref, unioning the results (de-duped on the raw alert
        # ``number``) so both workflow shapes are covered. Resolving the PR is
        # best-effort: any failure degrades to the branch-ref-only query.
        try:
            pr = self._get_pr(owner=owner, repo=repo, head=source_branch)
        except Exception:  # noqa: BLE001 — best-effort; fall back to branch ref
            pr = None
        refs = [f"refs/heads/{source_branch}"]
        if pr is not None:
            refs.insert(0, f"refs/pull/{pr['number']}/merge")

        seen: set[int] = set()
        raw_alerts: list[dict] = []
        for ref in refs:
            for a in self._fetch_alerts_for_ref(owner=owner, repo=repo, ref=ref):
                # Dedupe on the raw GitHub alert number BEFORE the parse loop
                # (the parsed dict drops the number). Alerts without a number
                # (defensive) are kept as-is.
                num = a.get("number") if isinstance(a, dict) else None
                if num is not None:
                    if num in seen:
                        continue
                    seen.add(num)
                raw_alerts.append(a)

        out: list[dict] = []
        for a in raw_alerts:
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
        """Return the logs of the failed jobs in workflow run *run_id*.

        :param run_id: GitHub Actions workflow-run id whose jobs to fetch.
        Concatenates (ANSI-stripped, failure-window-capped) logs for up to
        the first few failed-like jobs of the run into a single string;
        returns ``""`` when the run has no failed jobs.
        """
        owner, repo = self._owner_repo
        return self._fetch_workflow_job_logs(
            owner=owner,
            repo=repo,
            run_id=run_id,
        )

    def create_repo(
        self, *, name: str, owner: str, private: bool | None = None, description: str
    ) -> RepoInfo:
        """Create a new GitHub repository and return its :class:`RepoInfo`.

        :param name: repository name.
        :param owner: org/user namespace to create under (empty falls back
            to the authenticated user).
        :param private: whether the repo is private (defaults to config).
        :param description: repo description (clamped to GitHub's limit).
        Mutates remote state: creates the repo via the GitHub API. Raises
        :class:`NotConfiguredError` when ``enable_repo_creation`` is off.
        """
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
        """Fork an existing GitHub repository and return its :class:`RepoInfo`.

        :param source_owner: owner of the repo to fork.
        :param source_repo: name of the repo to fork.
        :param target_namespace: org to create the fork under (defaults to
            the authenticated account).
        Mutates remote state: creates the fork via the GitHub API. Raises
        :class:`NotConfiguredError` when ``enable_repo_creation`` is off.
        """
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
        """Delete remote *branch*, returning ``True`` once it is gone.

        :param branch: branch name to delete.
        Mutates remote state: issues a DELETE on the branch ref (resolved to
        the fork for cross-repo targets). Returns ``True`` when the branch is
        deleted or already absent, ``False`` on any other failure.
        """
        # For cross-repo targets the head branch lives on the fork,
        # not the upstream repo.  Resolve the fork owner/repo so the
        # DELETE goes to the right place instead of 404'ing on
        # upstream.
        if self._repo_config is not None:
            cct = getattr(self._repo_config, "cross_repo_target", None)
            if cct is not None and cct.fork_remote_url:
                fork_owner, fork_repo = _parse_owner_repo(cct.fork_remote_url)
                return self._delete_branch(
                    owner=fork_owner, repo=fork_repo, branch=branch
                )
        owner, repo = self._owner_repo
        return self._delete_branch(owner=owner, repo=repo, branch=branch)

    def list_branches(self) -> list[BranchInfo]:
        """Return all branches of the repo as :class:`BranchInfo` entries.

        Paginates the GitHub branches API and returns a ``list[BranchInfo]``
        (``name``, ``last_commit_at``, ``is_protected``). Returns ``[]`` on
        any API failure.
        """
        owner, repo = self._owner_repo
        return self._list_branches(owner=owner, repo=repo)

    def list_open_pr_branches(self) -> set[str]:
        """Return the set of head branch names that have an open PR.

        Paginates the open-PRs API and collects each PR's head ref. Returns
        a ``set[str]`` of branch names (empty on any API failure).
        """
        owner, repo = self._owner_repo
        return self._list_open_pr_branches(owner=owner, repo=repo)

    # --- HTTP seam (monkeypatched in tests) ---
    def _get_pr(self, *, owner: str, repo: str, head: str) -> dict | None:
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        # For cross-repo targets the head branch lives on the fork,
        # so the head filter must use the fork owner (not the upstream
        # owner passed in *owner*).  _head_owner resolves accordingly.
        head_owner = self._head_owner
        for retry in range(2):
            with self._http.client() as (c, api, headers):
                lst = c.get(
                    f"{api}/repos/{owner}/{repo}/pulls",
                    headers=headers,
                    params={"head": f"{head_owner}:{head}", "state": "all"},
                )
                if lst.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    continue
                lst.raise_for_status()
                items = lst.json()
                if not items:
                    return None
                num = items[0]["number"]
                d = c.get(f"{api}/repos/{owner}/{repo}/pulls/{num}", headers=headers)
                if d.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    continue
                d.raise_for_status()
                pr = d.json()
            return _parse_pr_detail(pr)
        return None

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
            r = self._http.delete(f"/repos/{owner}/{repo}/git/refs/heads/{branch}")
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
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        out: list[BranchInfo] = []
        for retry in range(2):
            hit_401 = False
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
                        if r.status_code == 401 and retry == 0:
                            invalidate_github_token(self.settings, self._repo_config)
                            time.sleep(2)
                            hit_401 = True
                            break
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
                if hit_401:
                    out.clear()
                    continue
                break  # success
            except Exception:
                return []
        return out

    # --- HTTP seam (monkeypatched in tests) ---
    def _list_open_pr_branches(self, *, owner: str, repo: str) -> set[str]:
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        out: set[str] = set()
        for retry in range(2):
            hit_401 = False
            try:
                with self._http.client() as (c, api, headers):
                    url = f"{api}/repos/{owner}/{repo}/pulls"
                    page = 1
                    while True:
                        r = c.get(
                            url,
                            headers=headers,
                            params={
                                "state": "open",
                                "per_page": 100,
                                "page": page,
                            },
                        )
                        if r.status_code == 401 and retry == 0:
                            invalidate_github_token(self.settings, self._repo_config)
                            time.sleep(2)
                            hit_401 = True
                            break
                        r.raise_for_status()
                        items = r.json()
                        for pr in items:
                            ref = (pr.get("head") or {}).get("ref")
                            if ref:
                                out.add(ref)
                        if len(items) < 100:
                            break
                        page += 1
                if hit_401:
                    out.clear()
                    continue
                break  # success
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
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        for retry in range(2):
            with self._http.client() as (c, api, headers):
                # 1. Fetch reviews (includes state field that list_pr_reviews drops).
                r = c.get(
                    f"{api}/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
                    headers=headers,
                    params={"per_page": 100},
                )
                if r.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    continue
                r.raise_for_status()
                reviews_raw = r.json()

                # 2. Fetch inline review comments.
                r2 = c.get(
                    f"{api}/repos/{owner}/{repo}/pulls/{pull_number}/comments",
                    headers=headers,
                    params={"per_page": 100},
                )
                if r2.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    continue
                r2.raise_for_status()
                comments_raw = r2.json()

                # 3. Fetch changed files.
                files = self._pr_files(
                    owner=owner,
                    repo=repo,
                    pull_number=pull_number,
                )

            # If we get here the client block succeeded.
            break

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
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        pr = self._get_pr(owner=owner, repo=repo, head=head)
        if pr is None:
            return None

        sha = pr.get("sha", "")
        if not sha:
            return None

        for retry in range(2):
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
                if cr_resp.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    continue
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
                if st_resp.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    continue
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

                return _derive_check_conclusion(
                    c, api, owner, repo, headers, check_runs
                )
        return None

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
        import time

        from .auth import invalidate_github_token  # lazy: avoid import cycle

        s = self.settings

        # 1. List jobs for the run (with 401 retry).
        for retry in range(2):
            with self._http.client() as (c, api, headers):
                jobs_resp = c.get(
                    f"{api}/repos/{owner}/{repo}/actions/runs/{run_id}/jobs",
                    headers=headers,
                    params={"status": "completed"},
                )
                if jobs_resp.status_code == 401 and retry == 0:
                    invalidate_github_token(self.settings, self._repo_config)
                    time.sleep(2)
                    continue
                jobs_resp.raise_for_status()
                jobs = jobs_resp.json().get("jobs", [])
            break

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

        # 3. Fetch logs for each failed job (with 401 retry per fetch).
        with self._http.client() as (c, api, headers):
            for j in failed_jobs:
                job_id = j["id"]
                job_name = j.get("name", f"job-{job_id}")
                # Each job-log fetch gets its own 401 retry.
                raw: str = ""
                for log_retry in range(2):
                    try:
                        log_resp = c.get(
                            f"{api}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                            headers=headers,
                            follow_redirects=True,
                        )
                        if log_resp.status_code == 401 and log_retry == 0:
                            invalidate_github_token(self.settings, self._repo_config)
                            time.sleep(2)
                            headers = self._http.regenerate_headers()
                            continue
                        log_resp.raise_for_status()
                        raw = log_resp.text
                    except httpx.HTTPStatusError:
                        sc = log_resp.status_code
                        if sc == 403:
                            raw = f"[log fetch failed for job {job_id}: HTTP 403 — App likely missing Actions:Read permission]"
                        else:
                            raw = f"[log fetch failed for job {job_id}: HTTP {sc}]"
                    except Exception as exc:
                        raw = (
                            f"[log fetch failed for job {job_id}: {type(exc).__name__}]"
                        )
                    else:
                        if not raw:
                            raw = f"[log fetch returned empty body for job {job_id}]"
                    break  # success or final attempt

                # Strip ANSI.
                clean = _ANSI_RE.sub("", raw)
                # Capture the window around the FIRST failure marker (not a
                # blind tail-cap) so an ``if: always()`` cascade — where a
                # downstream always-step re-errors with misleading input —
                # can't mask the step that actually failed first.
                clean = _capture_failure_window(
                    clean,
                    log_max,
                    failure_re=_LOG_FAILURE_RE,
                    tail_context=_LOG_FAILURE_TAIL_CONTEXT,
                )

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
    for ctx, _items in by_context.items():
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
    conclusion = cr.get("conclusion")
    if conclusion in _INCONCLUSIVE_CONCLUSIONS:
        # Superseded / no-verdict → wait for the authoritative run rather
        # than reporting a false failure (see _INCONCLUSIVE_CONCLUSIONS).
        return "pending"
    if conclusion in _FAILING_CONCLUSIONS:
        return "failure"
    return "neutral"


def _latest_definitive_runs(check_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse multiple runs of the SAME check name to one representative.

    A check context (e.g. ``ci (3.11) / tests``) can have several runs at
    one commit: GitHub's concurrency control ``cancelled`` the superseded
    run when a newer one started, so the same name carries both a
    ``cancelled`` AND a ``success`` run. ``_conclusion_for_check`` maps
    ``cancelled``→``pending`` (so a genuinely-cancelling churn isn't read as
    a false failure — see ``_INCONCLUSIVE_CONCLUSIONS``); but feeding BOTH
    runs to the aggregator makes the whole PR read ``pending`` forever even
    though the authoritative run is green — the ticket then sits in
    IMPLEMENT_COMPLETE and never merges (live: llmio c273/55f1/d932/fcf4).

    Per name, prefer the latest run with a DEFINITIVE conclusion
    (success/failure — not cancelled/stale/running); fall back to the
    latest run overall when only inconclusive/in-flight runs exist (so a
    still-churning check correctly stays pending). Ordering is by
    ``started_at`` (ISO strings sort chronologically).
    """
    by_name: dict[str, list[dict[str, Any]]] = {}
    for cr in check_runs:
        by_name.setdefault(cr.get("name", ""), []).append(cr)
    reps: list[dict[str, Any]] = []
    for runs in by_name.values():
        runs_sorted = sorted(runs, key=lambda r: r.get("started_at") or "")
        definitive = [
            r
            for r in runs_sorted
            if r.get("status", "") not in _PENDING_STATUSES
            and (r.get("conclusion") or "") not in _INCONCLUSIVE_CONCLUSIONS
        ]
        reps.append(definitive[-1] if definitive else runs_sorted[-1])
    return reps


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

    # Collapse same-name reruns so a superseded ``cancelled`` run doesn't
    # mask the authoritative ``success`` and pin the PR at pending forever.
    check_runs = _latest_definitive_runs(check_runs)

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
