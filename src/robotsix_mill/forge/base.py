"""Forge adapter contract.

The deliver stage is the *only* place the system touches an external
forge. An adapter pushes a branch and opens a merge/pull request,
returning its URL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ..config import RepoConfig, Settings


class NotConfiguredError(RuntimeError):
    """Raised when an optional forge capability (e.g. repo creation) is
    disabled by configuration."""


@dataclass
class RepoInfo:
    """Metadata about a repository returned by :meth:`Forge.create_repo`."""

    id: int
    name: str
    clone_url: str
    html_url: str


@dataclass
class BranchInfo:
    """Metadata about a remote branch returned by :meth:`Forge.list_branches`."""

    name: str
    last_commit_at: datetime  # timezone-aware (UTC)
    is_protected: bool


class Forge(ABC):
    """Abstract contract for forge adapters: open MR/PR, query status, reviews, and merge."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def open_merge_request(
        self,
        *,
        source_branch: str,
        title: str,
        body: str,
        head_repo: str | None = None,
    ) -> str:
        """Open an MR/PR for ``source_branch`` against
        ``settings.forge_target_branch``. Returns the MR/PR URL.

        ``head_repo`` opts into a cross-fork PR: when given as an
        ``"owner/repo"`` string, the head branch lives in that fork and
        the PR is opened against the upstream repo / ``base_branch``
        resolved from the repo's ``cross_repo_target``. When ``None``
        (the default), behaviour is the ordinary same-repo PR."""

    @abstractmethod
    def pr_status(self, *, source_branch: str) -> dict | None:
        """Status of the PR/MR for ``source_branch``:
        ``{"merged": bool, "state": "open"|"closed", "url": str,
        "mergeable": bool | None}`` or ``None`` if no PR/MR exists yet.

        ``mergeable`` is ``True`` when the PR has no conflicts with the
        target branch, ``False`` when it does, and ``None`` when the
        forge hasn't yet performed the check (treat as mergeable)."""

    @abstractmethod
    def pr_status_by_url(self, *, url: str) -> dict | None:
        """Status of the PR/MR identified by its recorded *url*
        (as stored in pr_urls.json), independent of whether the head
        branch still exists. Returns the same shape as ``pr_status``
        ({"merged": bool, "state": ..., "url": ..., "mergeable": ...,
        "number": ...}) or ``None`` when the url cannot be parsed or
        the PR/MR cannot be resolved."""

    @abstractmethod
    def check_status(self, *, source_branch: str) -> dict | None:
        """Return remote CI check-run status for the PR of *source_branch*.

        Returns ``None`` when no PR exists for the branch.
        When a PR exists, returns::

            {
                "conclusion": "success" | "failure" | "pending" | None,
                "failing": [
                    {
                        "name": str,
                        "summary": str | None,
                        "text": str | None,
                        "annotations": [
                            {"path": str, "start_line": int | None,
                             "message": str, "level": str}
                        ]
                    }
                ],
            }

        ``"success"`` = all completed checks pass.
        ``"failure"``  = at least one completed check has a non-success
                         conclusion.
        ``"pending"``  = at least one check is not yet complete and none
                         have failed.
        ``None``       = no checks exist at all.

        Summaries are capped at 2000 chars, text at 4000, annotations at
        20 per failing check (adapter-enforced truncation).
        """

    def commit_ci_conclusion(self, *, sha: str) -> dict | None:
        """Aggregate CI conclusion for an arbitrary commit SHA (no PR).

        Default returns ``None`` (CI status unavailable) — non-GitHub forges
        must opt-in by overriding.  Same return shape as ``check_status``:
        ``{"conclusion": "success"|"failure"|"pending"|None,
           "failing": [...], "pending": [...]}``.
        """
        return None

    @abstractmethod
    def pr_files(self, *, source_branch: str) -> list[dict]:
        """Return the file-list diff of the PR/MR for *source_branch*.

        Returns ``[]`` when no PR exists or files are unavailable.
        Each dict has:
        ``path`` — file path (str)
        ``status`` — "added" | "modified" | "removed" | "renamed"
        ``additions`` — lines added (int)
        ``deletions`` — lines deleted (int)
        """

    @abstractmethod
    def pr_review_status(self, *, source_branch: str) -> dict | None:
        """Return the aggregate review state of the PR for *source_branch*.

        Returns ``None`` when no PR exists for the branch.

        Return shape:
        ``state`` — one of ``"APPROVED"``, ``"CHANGES_REQUESTED"``,
            ``"COMMENTED"``, ``"DISMISSED"``, or ``"PENDING"`` (no
            reviews yet). Derived from the *latest* non-dismissed
            review; falls back to the latest dismissed review when all
            reviews are dismissed.
        ``comments`` — list of dicts, each with:
            ``body`` (str), ``path`` (str, ``""`` for review-body
            comments), ``line`` (int | None),
            ``review_state`` (the state of the review this comment
            belongs to: APPROVED/CHANGES_REQUESTED/COMMENTED/DISMISSED).
        ``files`` — list of changed file paths (str). Same source as
            :meth:`pr_files` but returned as a plain list of strings
            for convenience.
        """

    @abstractmethod
    def merge_pr(self, *, source_branch: str) -> dict:
        """Merge the PR for *source_branch* (squash merge).

        Returns ``{"merged": True, "reason": "..."}`` on success,
        ``{"merged": False, "reason": "..."}`` on failure. Must never
        raise for API-level failures (branch protection, not mergeable,
        conflict, network error) — catch and return a failure dict."""

    @abstractmethod
    def close_pr(self, *, source_branch: str) -> bool:
        """Close/decline the open PR for *source_branch* without merging.

        Returns ``True`` on success, ``False`` when the PR is not found
        or already closed.  Never raises.
        """

    @abstractmethod
    def post_pr_comment(self, *, source_branch: str, body: str) -> bool:
        """Post a plain comment on the open PR for *source_branch*.

        Returns ``True`` on success, ``False`` when the PR is not found.
        Never raises.
        """

    @abstractmethod
    def list_pr_reviews(self, *, source_branch: str) -> list[dict]:
        """Return formal PR reviews (approve/request-changes/comment).

        Returns ``[]`` when no PR exists for the branch.  Each dict has:
        ``id``, ``author``, ``created_at``, ``body`` (``""`` when the
        review was submitted without a body, never ``None``).
        """

    @abstractmethod
    def list_review_comments(self, *, source_branch: str) -> list[dict]:
        """Return inline code-review comments on the PR for *source_branch*.

        Returns ``[]`` when no PR exists for the branch.  Each dict has:
        ``id``, ``author``, ``created_at``, ``body``, ``file_path``,
        ``line`` (int or ``None``), ``diff_hunk``.
        """

    @abstractmethod
    def list_workflow_runs(
        self, *, branch: str | None = None, head_sha: str | None = None
    ) -> list[dict]:
        """List completed workflow runs, optionally filtered by branch or head SHA.

        Returns a list of dicts, each with keys:
        ``id``, ``name``, ``workflow_id``, ``head_sha``, ``conclusion``,
        ``html_url``, ``created_at``, ``event``, ``head_branch``, ``path``.
        """

    @abstractmethod
    def fetch_workflow_job_logs(self, *, run_id: int, full_log: bool = False) -> str:
        """Fetch the logs of all failed jobs in a workflow run.

        Returns concatenated, ANSI-stripped log text with job-name
        headers.  When *full_log* is ``False`` (default), the log is
        size-capped and windowed around the first failure marker;
        ``True`` returns the complete job logs (still ANSI-stripped).
        Returns an empty string when no failed jobs are found.
        """

    @abstractmethod
    def create_repo(
        self, *, name: str, owner: str, private: bool | None = None, description: str
    ) -> RepoInfo:
        """Create a new repository under *owner* and return its metadata.

        Must raise ``NotConfiguredError`` when repo creation is disabled
        by configuration (e.g. a feature flag).
        """

    @abstractmethod
    def fork_repo(
        self,
        *,
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> RepoInfo:
        """Fork *source_owner/source_repo* and return the new fork's metadata.

        When *target_namespace* is provided, the fork is created under that
        organization (GitHub) or namespace path/id (GitLab).  Otherwise the
        fork is created under the authenticated user's account.

        Must raise ``NotConfiguredError`` when repo creation is disabled
        by configuration (e.g. a feature flag).
        """

    def list_code_scanning_alerts(self, *, source_branch: str) -> list[dict]:
        """List OPEN code-scanning (e.g. CodeQL) alerts on *source_branch*.

        Concrete (not abstract) with a ``[]`` default — code-scanning is a
        GitHub feature, so non-GitHub forges inherit the no-op. CodeQL
        findings live in the security/code-scanning API, NOT the workflow
        job logs, so without this the CI-fix agent is blind to a CodeQL
        failure (it sees only "CodeQL: failure" with no detail).

        Each dict: ``rule`` (id), ``severity``, ``path``, ``line``,
        ``message``, ``url``, ``number`` (the raw alert number for
        dismissal), ``security_severity_level`` (``null`` or
        ``"low"/"medium"/"high"/"critical"``).
        """
        return []

    def dismiss_code_scanning_alert(
        self, *, number: int, reason: str, comment: str
    ) -> bool:
        """Dismiss a single code-scanning alert by its *number*.

        *reason* must be one of ``"false positive"``, ``"won't fix"``,
        or ``"used in tests"`` (GitHub's required enum — note the spaces,
        not underscores).  *comment* is an optional dismissal note.

        Returns ``True`` on success, ``False`` on any failure (not found,
        insufficient scope, network error).  Concrete default returns
        ``False`` — only GitHub implements this capability.
        """
        return False

    def list_dependabot_alerts(self) -> list[dict[str, Any]]:
        """List OPEN Dependabot vulnerability alerts on the repo.

        Concrete (not abstract) with a ``[]`` default — Dependabot is a
        GitHub feature, so non-GitHub forges inherit the no-op.

        Each dict: ``number``, ``ghsa_id``, ``cve_id``, ``severity``
        (``critical``/``high``/``medium``/``low``), ``package``,
        ``ecosystem``, ``manifest_path``, ``summary``, ``url``.
        """
        return []

    def update_branch(self, *, source_branch: str) -> dict:
        """Merge the PR's base branch into the PR branch (server-side) so its
        CI re-runs against the current base tip. Default: unsupported no-op."""
        return {"updated": False, "reason": "not supported"}

    def delete_branch(self, *, branch: str) -> bool:
        """Delete the remote head branch *branch* after merge.

        Returns True on success, False on any failure (branch already
        gone, 404/422, network error, insufficient scope). Must NEVER
        raise — catch all API-level failures and return False."""
        return False

    def list_branches(self) -> list[BranchInfo]:
        """List all remote branches with last-commit timestamp and
        protection flag. Returns [] on any failure. Must NEVER raise."""
        return []

    def list_open_pr_branches(self) -> set[str]:
        """Head branch names of all currently-open PRs/MRs. Returns an
        empty set on any failure. Must NEVER raise."""
        return set()


def _detect_forge_kind(remote_url: str) -> Literal["github", "gitlab"]:
    """Inspect a remote URL and return ``"github"`` or ``"gitlab"``.

    Heuristics:
    - ``github.com`` host → ``"github"`` (covers https + git@ scp-style)
    - ``gitlab.com`` host → ``"gitlab"``

    Custom domains (GHE, self-hosted GitLab, etc.) raise ``RuntimeError``
    because they are ambiguous — the operator must set ``FORGE_KIND``
    explicitly.
    """
    # Strip an optional "https://" prefix and any trailing "/" + ".git".
    cleaned = remote_url.strip()
    if cleaned.startswith("https://"):
        cleaned = cleaned[len("https://") :]
    elif cleaned.startswith("http://"):
        cleaned = cleaned[len("http://") :]

    # "git@<host>:<path>.git" → extract "<host>"
    if cleaned.startswith("git@"):
        # "git@host:path" → split on ":" after stripping "git@"
        colon = cleaned.find(":")
        if colon == -1:
            host = cleaned[4:]
        else:
            host = cleaned[4:colon]
    else:
        # https://host/... → split on "/"
        host = cleaned.split("/")[0]

    # Strip optional port — we only detect by hostname.
    host = host.split("@")[-1]  # "user@host" safety (unlikely but safe)
    host = host.split(":")[0]  # "host:port"

    if host == "github.com":
        return "github"
    if host == "gitlab.com":
        return "gitlab"

    raise RuntimeError(
        f"cannot auto-detect forge kind from {remote_url!r}; "
        "set FORGE_KIND explicitly (github or gitlab)"
    )


def get_forge(settings: Settings, repo_config: RepoConfig | None = None) -> Forge:
    """Resolve the configured forge adapter.

    When *repo_config* is provided, the forge adapter is built with
    that repo's remote URL so push/PR/merge operations target the
    correct repository.  The adapter itself receives the repo_config
    for token minting and remote resolution.

    Forge selection is per-repo: when *repo_config* carries a
    ``forge_remote_url``, the forge kind is detected from THAT url and
    overrides the global ``settings.forge_kind``.  This lets a single
    multi-repo deployment whose global ``forge_kind`` is ``"github"``
    still route a GitLab-hosted repo to :class:`GitLabForge` (and vice
    versa) based on its own remote.  If the per-repo URL is on a
    custom/ambiguous domain (``_detect_forge_kind`` raises), we fall
    back to the global kind instead of crashing.

    When ``forge_kind == "auto"``, the effective forge kind is detected
    from the remote URL (per-repo ``forge_remote_url`` if provided,
    else the global ``settings.forge_remote_url``).
    """
    kind = settings.forge_kind
    per_repo_url = repo_config.forge_remote_url if repo_config is not None else None
    if per_repo_url:
        try:
            # The per-repo remote URL is authoritative — it wins over the
            # global forge_kind so mixed-forge fleets route correctly.
            kind = _detect_forge_kind(per_repo_url)
        except RuntimeError:
            # Ambiguous/custom domain — keep the global kind (no new crash).
            kind = settings.forge_kind
    if kind == "auto":
        remote_url = per_repo_url or settings.forge_remote_url or ""
        kind = _detect_forge_kind(remote_url)
    if kind == "github":
        from .github import GitHubForge

        return GitHubForge(settings, repo_config=repo_config)
    if kind == "gitlab":
        from .gitlab import GitLabForge

        return GitLabForge(settings, repo_config=repo_config)
    raise RuntimeError(f"no forge configured (FORGE_KIND={kind!r}); cannot deliver")
