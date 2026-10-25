"""GitLab SAST / code-scanning alert operations — mixed into ``GitLabForge``.

Placeholder: GitLab Ultimate SAST API may expose code-scanning results
in a future version.  When that endpoint becomes available, implement
alert listing / filtering here following the GitHub
``GitHubForgeCodeScanningMixin`` pattern.

Expects ``self._http`` and ``self._resolve_project_id`` to exist on the
final class.
"""

from __future__ import annotations


class GitLabForgeCodeScanningMixin:
    """Code-scanning (SAST) alert operations — mixed into ``GitLabForge``."""
