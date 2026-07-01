"""GitLab forge adapter — package split from the monolithic ``gitlab.py``.

Sub-modules mirror the GitHub adapter architecture:
- ``core.py`` — main ``GitLabForge`` class, MR lifecycle, branches, repo CRUD
- ``ci.py`` — CI pipeline status, job log retrieval, failure analysis
- ``code_scanning.py`` — SAST report retrieval (placeholder)
- ``dependabot.py`` — dependency update alerts (placeholder)
- ``_pagination.py`` — shared pagination helper
"""

from .ci import _LOG_FAILURE_RE
from .core import GitLabForge, _build_headers, _parse_gitlab_project_path

__all__ = [
    "GitLabForge",
    "_build_headers",
    "_LOG_FAILURE_RE",
    "_parse_gitlab_project_path",
]
