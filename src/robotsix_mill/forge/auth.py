"""Resolve the effective GitHub token for push + PR.

``FORGE_AUTH=token`` → use ``FORGE_TOKEN`` (a PAT) as-is.
``FORGE_AUTH=app``   → mint a short-lived GitHub App *installation*
access token (JWT signed with the App private key → installation
token), so the PR is authored by ``<app-slug>[bot]`` — the
robotsix-project bot identity, without GitHub Actions.

``_mint_installation_token`` is the network/JWT seam (monkeypatched in
tests, so pyjwt/httpx aren't needed for the token-auth path or the
suite). Minted tokens (~1 h TTL) are cached so one deliver doesn't mint
twice (push + PR).

Per-repo installations: when a ``RepoConfig`` with ``forge_remote_url``
is provided, the target owner/repo is derived from that repo's remote
instead of the global ``settings.forge_remote_url``.  This lets
different repos under the same (or different) GitHub Apps mint
installation tokens for their respective remotes.
"""

from __future__ import annotations

import logging
import time

from ..config import RepoConfig, Settings, get_secrets

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[str, float]] = {}


class GitHubAppNotInstalledError(RuntimeError):
    """Raised when the GitHub App is not installed on a repository.

    The ``/repos/{owner}/{repo}/installation`` endpoint returned 404 —
    the App must be installed on the target repo before it can mint
    installation tokens.
    """

    def __init__(self, owner: str, repo: str) -> None:
        self.owner = owner
        self.repo = repo
        super().__init__(
            f"GitHub App not installed on {owner}/{repo} — "
            f"install the App on the repository or remove it from the "
            f"registered repos"
        )


def _private_key() -> str:
    if get_secrets().github_app_private_key_path:
        with open(get_secrets().github_app_private_key_path, encoding="utf-8") as f:
            return f.read()
    key = get_secrets().github_app_private_key or ""
    # allow a single-line env value with literal "\n"
    return key.replace("\\n", "\n")


def _resolve_remote_url(
    settings: Settings, repo_config: RepoConfig | None = None
) -> str:
    """Return the effective forge remote URL.

    When *repo_config* has a ``forge_remote_url``, use it; otherwise
    fall back to the global ``settings.forge_remote_url``.
    """
    if repo_config is not None and getattr(repo_config, "forge_remote_url", None):
        return repo_config.forge_remote_url
    return settings.forge_remote_url or ""


def _mint_installation_token(
    settings: Settings, repo_config: RepoConfig | None = None
) -> tuple[str, float]:
    """Returns (token, unix_expiry). Seam: tests monkeypatch this."""
    import httpx
    import jwt

    from .github import _parse_owner_repo  # lazy: avoid import cycle

    api = settings.github_api_url.rstrip("/")
    remote_url = _resolve_remote_url(settings, repo_config)
    owner, repo = _parse_owner_repo(remote_url)
    now = int(time.time())
    bearer = jwt.encode(
        {"iat": now - 60, "exp": now + 9 * 60, "iss": get_secrets().github_app_id},
        _private_key(),
        algorithm="RS256",
    )
    h = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=30) as c:
        inst = c.get(f"{api}/repos/{owner}/{repo}/installation", headers=h)
        if inst.status_code == 404:
            raise GitHubAppNotInstalledError(owner, repo)
        inst.raise_for_status()
        iid = inst.json()["id"]
        tok = c.post(f"{api}/app/installations/{iid}/access_tokens", headers=h)
        tok.raise_for_status()
        data = tok.json()
    # expires_at is ISO; just cache for 50 min regardless
    return data["token"], time.time() + 50 * 60


def gitlab_token() -> str:
    """Return the GitLab PAT from secrets.

    Raises ``RuntimeError`` when ``FORGE_TOKEN`` is not configured.
    """
    token = get_secrets().forge_token
    if not token:
        raise RuntimeError("FORGE_TOKEN not set")
    return token


def invalidate_github_token(
    settings: Settings, repo_config: RepoConfig | None = None
) -> None:
    """Remove the cached installation token for *settings* + *repo_config*.

    Safe to call when no entry exists (``.pop(ck, None)``).  After
    invalidation the next ``github_token(...)`` call will mint a fresh
    token from the GitHub API.
    """
    remote_url = _resolve_remote_url(settings, repo_config)
    ck = f"{get_secrets().github_app_id}:{remote_url}"
    _cache.pop(ck, None)
    logger.debug("invalidate_github_token key=%s", ck)


def invalidate_and_backoff(
    settings: Settings, repo_config: RepoConfig | None = None
) -> None:
    """Invalidate the cached GitHub token and sleep 2 s before retrying.

    Combines ``invalidate_github_token()`` with a 2-second backoff.
    Use this in 401 retry loops inside ``_ApiClient.client()`` blocks
    (the ``_do()`` path already applies its own sleep via ``_on_401``).
    """
    invalidate_github_token(settings, repo_config)
    time.sleep(2)


def github_token(settings: Settings, repo_config: RepoConfig | None = None) -> str:
    """Return a forge auth token: either a static FORGE_TOKEN from secrets or a short-lived GitHub App installation token."""
    if settings.forge_auth != "app":
        if not get_secrets().forge_token:
            raise RuntimeError("FORGE_TOKEN not set")
        return get_secrets().forge_token

    if not get_secrets().github_app_id or not (
        get_secrets().github_app_private_key
        or get_secrets().github_app_private_key_path
    ):
        raise RuntimeError(
            "FORGE_AUTH=app needs GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY[_PATH]"
        )
    remote_url = _resolve_remote_url(settings, repo_config)
    ck = f"{get_secrets().github_app_id}:{remote_url}"
    cached = _cache.get(ck)
    if cached and cached[1] - 60 > time.time():
        remaining = cached[1] - time.time()
        logger.debug("github_token cache hit key=%s remaining_ttl=%.0fs", ck, remaining)
        return cached[0]
    logger.debug("github_token cache miss key=%s — minting fresh token", ck)
    token, expiry = _mint_installation_token(settings, repo_config=repo_config)
    _cache[ck] = (token, expiry)
    return token
