"""Forge adapter contract.

The deliver stage is the *only* place the system touches an external
forge. An adapter pushes a branch and opens a merge/pull request,
returning its URL.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import Settings


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


def get_forge(settings: Settings) -> Forge:
    """Resolve the configured forge adapter."""
    kind = settings.forge_kind
    if kind == "github":
        from .github import GitHubForge

        return GitHubForge(settings)
    if kind == "gitlab":
        from .gitlab import GitLabForge

        return GitLabForge(settings)
    raise RuntimeError(
        f"no forge configured (FORGE_KIND={kind!r}); cannot deliver"
    )
