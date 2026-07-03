"""GitHub forge adapter — open a Pull Request for an already-pushed
branch via the GitHub REST API. The branch push is done by the deliver
stage (it owns the repo dir); this only does the API call.

Split from a monolithic file into domain-specific mixins:
``github_pr.py`` (PR/branch ops), ``github_ci.py`` (CI/workflow),
``github_code_scanning.py`` (code scanning alerts).

This module keeps the main ``GitHubForge`` class (which inherits from
all three mixins), shared helpers, and repo-CRUD operations
(create / fork).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from ._http import _ApiClient
from .base import Forge, NotConfiguredError, RepoInfo
from .github_ci import GitHubForgeCIMixin
from .github_code_scanning import GitHubForgeCodeScanningMixin
from .github_dependabot import GitHubForgeDependabotMixin
from .github_pr import GitHubForgePRMixin


# ---------------------------------------------------------------------------
# Shared module-level helpers
# ---------------------------------------------------------------------------

_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

# GitHub's hard cap on a repository description (POST /user|orgs/.../repos).
_MAX_REPO_DESCRIPTION = 350


def _parse_iso_utc(value: str | None) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC datetime.

    Accepts a trailing ``Z`` (GitHub's UTC marker). Naive timestamps are
    assumed UTC; aware ones are converted to UTC. Returns the Unix epoch
    (UTC) when *value* is missing or unparseable.
    """
    if not value:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _build_headers(token: str) -> dict:
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


def _parse_repo_info(r: dict) -> RepoInfo:
    """Extract ``RepoInfo`` from a GitHub ``POST /repos`` response dict."""
    return RepoInfo(
        id=r["id"],
        name=r["name"],
        clone_url=r["clone_url"],
        html_url=r["html_url"],
    )


# ---------------------------------------------------------------------------
# Main forge class
# ---------------------------------------------------------------------------


class GitHubForge(
    GitHubForgePRMixin,
    GitHubForgeCIMixin,
    GitHubForgeCodeScanningMixin,
    GitHubForgeDependabotMixin,
    Forge,
):
    """GitHub adapter — opens PRs, queries checks/reviews/files, merges,
    fetches workflow logs, and manages code-scanning and Dependabot alerts
    via the GitHub REST API."""

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
        push (to the fork) is driven separately by the deliver stage."""
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
    # Repo CRUD
    # ------------------------------------------------------------------

    # --- HTTP seam (monkeypatched in tests) ---
    def _create_repo(
        self,
        *,
        name: str,
        owner: str,
        private: bool | None = None,
        description: str,
    ) -> RepoInfo:
        if private is None:
            private = self.settings.repo_visibility_default == "private"

        from ..config import get_secrets

        from .auth import github_token  # lazy: avoid import cycle

        s = self.settings
        # Repo creation needs a token that can create repos. GitHub App
        # installation tokens cannot create repositories under a personal
        # account, so prefer a dedicated repo-creation PAT when configured;
        # fall back to the normal (App or token) auth otherwise.
        token: str = ""
        custom_headers: dict[str, str] = {}

        def _mk_headers() -> dict[str, str]:
            nonlocal token, custom_headers
            token = get_secrets().forge_repo_create_token or github_token(
                s, repo_config=self._repo_config
            )
            custom_headers = _build_headers(token)
            return custom_headers

        def _create_attempt(c, api):
            # Primary: create under org
            org_url = f"{api}/orgs/{owner}/repos"
            r = c.post(org_url, headers=custom_headers, json=payload)
            if r.status_code == 201:
                return r, False  # response, not_a_401
            if r.status_code in (403, 404):
                user_url = f"{api}/user/repos"
                r2 = c.post(user_url, headers=custom_headers, json=payload)
                if r2.status_code == 201:
                    return r2, False
                r = r2
            if r.status_code == 401:
                return r, True  # signal 401
            return r, False

        payload = {
            "name": name,
            "private": private,
            # GitHub rejects repo descriptions over 350 chars with a 422;
            # the meta-agent's draft body easily exceeds that, so clamp.
            "description": _clamp_repo_description(description),
            "auto_init": False,
        }

        for _retry, c, api, _headers in self._http.retrying_client(
            headers_factory=_mk_headers,
        ):
            r, is_401 = _create_attempt(c, api)
            if is_401:
                continue
            break

        # Post-request error handling (original logic preserved).
        if r.status_code == 201:
            return _parse_repo_info(r.json())
        # 422 handling — no retry; repo creation races don't apply
        if r.status_code == 422:
            err_text = r.text or ""
            if "name already exists" in err_text.lower():
                # Re-run safety: a prior scaffold attempt may have created
                # the repo before failing later (e.g. on the initial
                # push). If the existing repo is EMPTY (no commits), reuse
                # it so the scaffold's force-push completes the job; only a
                # repo with real content is treated as a genuine conflict.
                with self._http.client() as (c, api, _headers):
                    existing = self._reuse_if_empty(c, api, custom_headers, owner, name)
                if existing is not None:
                    return existing
                raise RuntimeError(
                    f"Repository '{name}' already exists under '{owner}' "
                    f"and is not empty — refusing to overwrite"
                )
            raise RuntimeError(
                f"GitHub repo create failed: {r.status_code} {r.text[:300]}"
            )
        if (
            r.status_code == 403
            and "not accessible by integration" in (r.text or "").lower()
        ):
            raise RuntimeError(
                "GitHub repo create failed: 403 Resource not accessible by "
                "integration. A GitHub App installation token cannot create "
                "repositories under a personal account — set "
                "`forge_repo_create_token` in secrets to a PAT with "
                "repo-creation rights (classic: `repo` scope; fine-grained: "
                "Administration:Read and write on the target account)."
            )
        raise RuntimeError(f"GitHub repo create failed: {r.status_code} {r.text[:300]}")

    def _reuse_if_empty(self, c, api, headers, owner, name) -> RepoInfo | None:
        """Return the existing repo's ``RepoInfo`` iff it exists and is EMPTY
        (no commits), else ``None``.

        Used to make repo creation re-run-safe: GitHub's commits endpoint
        returns 409 (``Git Repository is empty``) for a repo with no commits.
        An empty repo is safe for the scaffold to force-push into; a repo with
        real content is not, so we signal a genuine conflict by returning None.

        When *owner* is empty the create fell back to ``/user/repos`` (the
        authenticated user), so resolve that login via ``/user`` — otherwise
        the ``/repos//{name}`` lookup 404s and a re-run wrongly blocks.
        """
        if not owner:
            u = c.get(f"{api}/user", headers=headers)
            owner = u.json().get("login", "") if u.status_code == 200 else ""
            if not owner:
                return None
        repo_url = f"{api}/repos/{owner}/{name}"
        rr = c.get(repo_url, headers=headers)
        if rr.status_code != 200:
            return None
        commits = c.get(f"{repo_url}/commits", headers=headers, params={"per_page": 1})
        # 409 = "Git Repository is empty"; an empty 200 list is empty too.
        is_empty = commits.status_code == 409 or (
            commits.status_code == 200 and not commits.json()
        )
        return _parse_repo_info(rr.json()) if is_empty else None

    # --- HTTP seam (monkeypatched in tests) ---
    def _fork_repo(
        self,
        *,
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> RepoInfo:
        from ..config import get_secrets

        from .auth import github_token  # lazy: avoid import cycle

        s = self.settings
        url = f"/repos/{source_owner}/{source_repo}/forks"
        payload: dict = {}
        if target_namespace is not None:
            payload["organization"] = target_namespace

        def _mk_headers() -> dict[str, str]:
            token = get_secrets().forge_repo_create_token or github_token(
                s, repo_config=self._repo_config
            )
            return _build_headers(token)

        for _retry, c, api, headers in self._http.retrying_client(
            headers_factory=_mk_headers,
        ):
            r = c.post(f"{api}{url}", headers=headers, json=payload)
            if r.status_code == 401:
                continue
            break

        if r.status_code in (200, 201, 202):
            return _parse_repo_info(r.json())
        if (
            r.status_code == 403
            and "not accessible by integration" in (r.text or "").lower()
        ):
            raise RuntimeError(
                "GitHub fork failed: 403 Resource not accessible by "
                "integration. A GitHub App installation token cannot fork "
                "repositories under a personal account — set "
                "`forge_repo_create_token` in secrets to a PAT with "
                "repo-creation rights (classic: `repo` scope; fine-grained: "
                "Administration:Read and write on the target account)."
            )
        raise RuntimeError(f"GitHub fork failed: {r.status_code} {r.text[:300]}")

    def create_repo(
        self, *, name: str, owner: str, private: bool | None = None, description: str
    ) -> RepoInfo:
        """Create a new GitHub repository and return its :class:`RepoInfo`.

        :param name: repository name.
        :param owner: org/user namespace to create under (empty falls back
            to the authenticated user).
        :param private: whether the repo is private (defaults to config).
        :param description: repo description (clamped to GitHub's limit).
        Mutates remote state: creates the repo via the GitHub API. Raises
        :class:`NotConfiguredError` when ``enable_repo_creation`` is off.
        """
        if not self.settings.enable_repo_creation:
            raise NotConfiguredError(
                "Repo creation is disabled. Set enable_repo_creation=True "
                "and verify the GitHub App installation has the "
                "Administration:Read and write permission (or equivalent "
                "PAT scope)."
            )
        return self._create_repo(
            name=name,
            owner=owner,
            private=private,
            description=description,
        )

    def fork_repo(
        self,
        *,
        source_owner: str,
        source_repo: str,
        target_namespace: str | None = None,
    ) -> RepoInfo:
        """Fork an existing GitHub repository and return its :class:`RepoInfo`.

        :param source_owner: owner of the repo to fork.
        :param source_repo: name of the repo to fork.
        :param target_namespace: org to create the fork under (defaults to
            the authenticated account).
        Mutates remote state: creates the fork via the GitHub API. Raises
        :class:`NotConfiguredError` when ``enable_repo_creation`` is off.
        """
        if not self.settings.enable_repo_creation:
            raise NotConfiguredError(
                "Repo creation is disabled. Set enable_repo_creation=True "
                "and verify the GitHub App installation has the "
                "Administration:Read and write permission (or equivalent "
                "PAT scope)."
            )
        return self._fork_repo(
            source_owner=source_owner,
            source_repo=source_repo,
            target_namespace=target_namespace,
        )

    def update_repo(self, *, owner: str, repo: str, description: str) -> bool:
        """Update an existing GitHub repository's metadata (description).

        :param owner: org/user that owns the repo.
        :param repo: repository name.
        :param description: new repo description (clamped to GitHub's limit).
        Returns ``True`` on success, ``False`` on any API failure. Raises
        :class:`NotConfiguredError` when ``enable_repo_creation`` is off.
        """
        if not self.settings.enable_repo_creation:
            raise NotConfiguredError(
                "Repo metadata updates are disabled. Set enable_repo_creation=True "
                "and verify the GitHub App installation has the "
                "Administration:Read and write permission (or equivalent "
                "PAT scope)."
            )
        return self._update_repo(owner=owner, repo=repo, description=description)

    def get_repo_description(self, *, owner: str, repo: str) -> str:
        """Return the current description of *owner/repo* or ``""`` on failure.

        Uses the same token resolution as ``_update_repo`` but performs a
        GET instead of a PATCH. Must NEVER raise.
        """
        return self._get_repo_description(owner=owner, repo=repo)

    # --- HTTP seam (monkeypatched in tests) ---
    def _get_repo_description(self, *, owner: str, repo: str) -> str:
        from ..config import get_secrets

        from .auth import github_token

        s = self.settings
        token = get_secrets().forge_repo_create_token or github_token(
            s, repo_config=self._repo_config
        )
        custom_headers = _build_headers(token)

        with self._http.client() as (c, api, _headers):
            r = c.get(
                f"{api}/repos/{owner}/{repo}",
                headers=custom_headers,
            )
        if r.status_code == 200:
            return (r.json().get("description") or "").strip()
        return ""

    def _update_repo(self, *, owner: str, repo: str, description: str) -> bool:
        from ..config import get_secrets

        from .auth import github_token

        s = self.settings
        token = get_secrets().forge_repo_create_token or github_token(
            s, repo_config=self._repo_config
        )
        custom_headers = _build_headers(token)
        payload = {"description": _clamp_repo_description(description)}

        with self._http.client() as (c, api, _headers):
            r = c.patch(
                f"{api}/repos/{owner}/{repo}",
                headers=custom_headers,
                json=payload,
            )
        return r.status_code == 200
