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
        """Push ``source_branch`` to the remote and open an MR/PR against
        ``settings.forge_target_branch``. Returns the MR/PR URL."""


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
