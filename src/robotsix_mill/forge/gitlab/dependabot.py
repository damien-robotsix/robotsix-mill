"""GitLab dependency-list / vulnerability alert operations — mixed into
``GitLabForge``.

Placeholder: the GitLab dependency list API
(``/projects/:id/dependencies``) may expose vulnerability alerts in a
future version.  When that endpoint becomes available, implement alert
listing / filtering here following the GitHub
``GitHubForgeDependabotMixin`` pattern.

Expects ``self._http`` and ``self._resolve_project_id`` to exist on the
final class.
"""

from __future__ import annotations


class GitLabForgeDependabotMixin:
    """Dependency update alert operations — mixed into ``GitLabForge``."""
