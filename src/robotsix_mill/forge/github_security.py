"""GitHub security-feature mixin — enable repo-level security settings.

Split from ``github.py``.  Defines ``GitHubForgeSecurityMixin`` that
``GitHubForge`` inherits from.

GitHub has no direct REST toggle for the Dependency Graph — it enables
implicitly when vulnerability alerts are turned on.  This module exposes
the two explicit toggles (vulnerability alerts + automated security fixes)
and a convenience ``ensure_dependency_graph_enabled()`` entry point that
the maintenance agent can call to enable the full security suite.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class GitHubForgeSecurityMixin:
    """Security-feature operations — mixed into ``GitHubForge``.

    Expects ``self._http`` and ``self._owner_repo`` to exist on the final
    class.
    """

    def enable_vulnerability_alerts(self) -> bool:
        """Enable Dependabot vulnerability alerts for the repo.

        PUT /repos/{owner}/{repo}/vulnerability-alerts

        Returns ``True`` on success (204), ``False`` on any failure
        (403 = token lacks permission, 404 = not found, transport error).
        Must NEVER raise.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        try:
            r = self._http.put(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/vulnerability-alerts",
            )
            # GitHub returns 204 No Content on success.
            if r.status_code == 204:
                return True
            r.raise_for_status()
            return True
        except Exception:  # noqa: BLE001 — best-effort, never fatal
            return False

    def enable_automated_security_fixes(self) -> bool:
        """Enable automated security fixes (Dependabot auto-fix PRs) for the repo.

        PUT /repos/{owner}/{repo}/automated-security-fixes

        Returns ``True`` on success (204), ``False`` on any failure
        (403 = token lacks permission, 404 = not found, transport error).
        Must NEVER raise.
        """
        owner, repo = self._owner_repo  # type: ignore[attr-defined]
        try:
            r = self._http.put(  # type: ignore[attr-defined]
                f"/repos/{owner}/{repo}/automated-security-fixes",
            )
            if r.status_code == 204:
                return True
            r.raise_for_status()
            return True
        except Exception:  # noqa: BLE001 — best-effort, never fatal
            return False

    def ensure_dependency_graph_enabled(self) -> bool:
        """Enable the dependency graph + vulnerability alerts on the repo.

        There is no direct REST endpoint for the Dependency Graph — it
        enables implicitly when vulnerability alerts are turned on.
        This convenience method calls :meth:`enable_vulnerability_alerts`
        and returns its result.

        Returns ``True`` on success, ``False`` on any failure.  Must
        NEVER raise.
        """
        return self.enable_vulnerability_alerts()
