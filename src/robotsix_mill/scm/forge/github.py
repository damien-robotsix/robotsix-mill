"""GitHub forge adapter — open a Pull Request for an already-pushed
branch via the GitHub REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.

Split from a monolithic file into domain-specific mixins:
``github_pr.py`` (PR lifecycle: create/merge/close/reopen/comment),
``github_pr_status.py`` (PR status/files/listing queries),
``github_branch.py`` (branch update/delete/listing),
``github_pr_reviews.py`` (PR review & comment handling),
``github_ci.py`` (CI/workflow),
``github_code_scanning.py`` (code scanning alerts).

This module keeps the main ``GitHubForge`` class (which inherits from
all the mixins), shared helpers, and repo-metadata operations
(description read / update).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ._http import _ApiClient
from .base import Forge
from .github_branch import GitHubForgeBranchMixin
from .github_ci import GitHubForgeCIMixin
from .github_code_scanning import GitHubForgeCodeScanningMixin
from .github_dependabot import GitHubForgeDependabotMixin
from .github_pr import GitHubForgePRMixin
from .github_pr_reviews import GitHubForgePRReviewsMixin
from .github_pr_status import GitHubForgePRStatusMixin
from .github_security import GitHubForgeSecurityMixin

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared module-level helpers
# ---------------------------------------------------------------------------

_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

# GitHub's hard cap on a repository description (PATCH /repos/...).
_MAX_REPO_DESCRIPTION = 350


def _build_headers(token: str) -> dict[str, Any]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_owner_repo(remote_url: str) -> tuple[str, str]:
    m = _REMOTE_RE.search(remote_url or "")
    if not m:
        raise RuntimeError(f"cannot parse owner/repo from {remote_url!r}")
    return m.group("owner"), m.group("repo")


def _clamp_repo_description(description: str) -> str:
    """Clamp *description* to GitHub's 350-char repo-description limit.

    A longer value yields a 422 ``description cannot be more than 350
    characters``. When truncating, end with an ellipsis so the cut is
    visible; collapse newlines (GitHub stores descriptions single-line).
    """
    text = " ".join((description or "").split())
    if len(text) <= _MAX_REPO_DESCRIPTION:
        return text
    return text[: _MAX_REPO_DESCRIPTION - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# Main forge class
# ---------------------------------------------------------------------------


class GitHubForge(
    GitHubForgePRMixin,
    GitHubForgePRStatusMixin,
    GitHubForgeBranchMixin,
    GitHubForgePRReviewsMixin,
    GitHubForgeCIMixin,
    GitHubForgeCodeScanningMixin,
    GitHubForgeDependabotMixin,
    GitHubForgeSecurityMixin,
    Forge,
):
    """GitHub adapter — opens PRs, queries checks/reviews/files, merges,
    fetches workflow logs, and manages code-scanning and Dependabot alerts
    via the GitHub REST API.
    """

    def __init__(self, settings, repo_config=None):
        super().__init__(settings)
        self._repo_config = repo_config
        from .auth import (  # lazy: avoid import cycle with auth.py
            github_token,
            invalidate_github_token,
        )

        self._http = _ApiClient(
            settings,
            repo_config,
            "github_api_url",
            lambda s, rc: _build_headers(github_token(s, repo_config=rc)),
        )
        if settings.forge_auth == "app":
            self._http._on_401 = lambda: invalidate_github_token(
                self.settings, self._repo_config
            )

    @property
    def _remote_url(self) -> str:
        """Effective remote URL: per-repo override, else global setting.

        For a repo with a ``cross_repo_target`` the effective remote is
        the *upstream* repo PRs are opened against — so PR create,
        status polling and merge all naturally target upstream. The
        push (to the fork) is driven separately by the deliver stage.
        """
        if self._repo_config is not None:
            cct = getattr(self._repo_config, "cross_repo_target", None)
            if cct is not None and cct.upstream_remote_url:
                return cct.upstream_remote_url
            remote = getattr(self._repo_config, "forge_remote_url", None)
            if remote:
                return remote
        return self.settings.forge_remote_url or ""

    @property
    def _owner_repo(self) -> tuple[str, str]:
        return _parse_owner_repo(self._remote_url)

    @property
    def _head_owner(self) -> str:
        """Owner of the head branch for PR lookups.

        For cross-repo targets the head lives on the fork, so this
        returns the fork owner; otherwise it's the same as the
        upstream/remote owner.  Used by ``_get_pr`` to build the
        ``head=<owner>:<branch>`` filter so the lookup finds the PR
        whose head branch is owned by the fork, not the upstream.
        """
        if self._repo_config is not None:
            cct = getattr(self._repo_config, "cross_repo_target", None)
            if cct is not None and cct.fork_remote_url:
                fork_owner, _ = _parse_owner_repo(cct.fork_remote_url)
                return fork_owner
        return self._owner_repo[0]

    # ------------------------------------------------------------------
    # Repo metadata
    # ------------------------------------------------------------------

    def update_repo(self, *, owner: str, repo: str, description: str) -> bool:
        """Update an existing GitHub repository's metadata (description).

        :param owner: org/user that owns the repo.
        :param repo: repository name.
        :param description: new repo description (clamped to GitHub's limit).
        Returns ``True`` on success, ``False`` on any API failure.
        """
        return self._update_repo(owner=owner, repo=repo, description=description)

    def get_repo_description(self, *, owner: str, repo: str) -> str:
        """Return the current description of *owner/repo* or ``""`` on failure.

        Uses the same token resolution as ``_update_repo`` but performs a
        GET instead of a PATCH. Must NEVER raise.
        """
        return self._get_repo_description(owner=owner, repo=repo)

    # --- HTTP seam (monkeypatched in tests) ---
    def _repo_metadata_headers(self) -> dict[str, Any]:
        """Auth headers for repo-metadata calls: the forge (App) token only.

        Repo metadata uses the same token as every other forge call. A
        separate PAT used to take precedence here; when it went stale
        (``401 Bad credentials``) it broke ``repo_description_sync`` while
        the App token would have served both calls.
        """
        from .auth import github_token

        return _build_headers(
            github_token(self.settings, repo_config=self._repo_config)
        )

    def _repo_metadata_request(
        self,
        method: str,
        owner: str,
        repo: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Issue ``method /repos/{owner}/{repo}`` and return the response."""
        custom_headers = self._repo_metadata_headers()
        with self._http.client() as (c, api, _headers):
            url = f"{api}/repos/{owner}/{repo}"
            if method == "PATCH":
                return c.patch(url, headers=custom_headers, json=json)
            return c.get(url, headers=custom_headers)

    def _get_repo_description(self, *, owner: str, repo: str) -> str:
        r = self._repo_metadata_request("GET", owner, repo)
        if r is not None and r.status_code == 200:
            return (r.json().get("description") or "").strip()
        return ""

    def _update_repo(self, *, owner: str, repo: str, description: str) -> bool:
        payload = {"description": _clamp_repo_description(description)}
        r = self._repo_metadata_request("PATCH", owner, repo, json=payload)
        if r is None or r.status_code != 200:
            # Surface the real cause (403 = token lacks Administration:write,
            # 404 = repo not visible to the token, ...) instead of a bare False.
            log.warning(
                "update_repo PATCH repos/%s/%s failed: HTTP %s: %s",
                owner,
                repo,
                getattr(r, "status_code", "n/a"),
                (getattr(r, "text", "") or "")[:500],
            )
            return False
        return True
