"""Forge adapter contract.

The deliver stage is the *only* place the system touches an external
forge. An adapter pushes a branch and opens a merge/pull request,
returning its URL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import RepoConfig, Settings


class Forge(ABC):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def open_merge_request(
        self, *, source_branch: str, title: str, body: str
    ) -> str:
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


def get_forge(
    settings: Settings, repo_config: RepoConfig | None = None
) -> Forge:
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
    raise RuntimeError(
        f"no forge configured (FORGE_KIND={kind!r}); cannot deliver"
    )
