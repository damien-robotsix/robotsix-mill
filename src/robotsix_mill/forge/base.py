"""Forge adapter contract.

The deliver stage is the *only* place the system touches an external
forge. An adapter pushes a branch and opens a merge/pull request,
returning its URL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

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


class Forge(ABC):
    """Abstract contract for forge adapters: open MR/PR, query status, reviews, and merge."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def open_merge_request(self, *, source_branch: str, title: str, body: str) -> str:
        """Open an MR/PR for ``source_branch`` against
        ``settings.forge_target_branch``. Returns the MR/PR URL."""

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
    def list_pr_comments(self, *, source_branch: str) -> list[dict]:
        """Return general PR conversation comments for *source_branch*.

        Returns ``[]`` when no PR exists for the branch.  Each dict has:
        ``id``, ``author`` (login string), ``created_at`` (ISO 8601),
        ``body``.
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
        ``html_url``, ``created_at``.
        """

    @abstractmethod
    def fetch_workflow_job_logs(self, *, run_id: int) -> str:
        """Fetch the logs of all failed jobs in a workflow run.

        Returns concatenated, ANSI-stripped, size-capped log text with
        job-name headers.  Returns an empty string when no failed jobs
        are found.
        """

    @abstractmethod
    def create_repo(
        self, *, name: str, owner: str, private: bool, description: str
    ) -> RepoInfo:
        """Create a new repository under *owner* and return its metadata.

        Must raise ``NotConfiguredError`` when repo creation is disabled
        by configuration (e.g. a feature flag).
        """


def get_forge(settings: Settings, repo_config: RepoConfig | None = None) -> Forge:
    """Resolve the configured forge adapter.

    When *repo_config* is provided, the forge adapter is built with
    that repo's remote URL so push/PR/merge operations target the
    correct repository.  The adapter itself receives the repo_config
    for token minting and remote resolution.
    """
    kind = settings.forge_kind
    if kind == "github":
        from .github import GitHubForge

        return GitHubForge(settings, repo_config=repo_config)
    if kind == "gitlab":
        from .gitlab import GitLabForge

        return GitLabForge(settings, repo_config=repo_config)
    raise RuntimeError(f"no forge configured (FORGE_KIND={kind!r}); cannot deliver")
