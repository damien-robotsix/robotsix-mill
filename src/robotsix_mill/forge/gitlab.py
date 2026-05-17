"""GitLab forge adapter. STUB — to be implemented."""

from __future__ import annotations

from .base import Forge


class GitLabForge(Forge):
    def open_merge_request(
        self, *, source_branch: str, title: str, body: str
    ) -> str:
        raise NotImplementedError("GitLab forge adapter not implemented yet")
