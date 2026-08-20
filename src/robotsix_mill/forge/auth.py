"""Resolve the effective GitHub token for push + PR.

``FORGE_AUTH=token`` → use ``FORGE_TOKEN`` (a PAT) as-is.
``FORGE_AUTH=app``   → mint a short-lived GitHub App *installation*
access token (JWT signed with the App private key → installation
token), so the PR is authored by ``<app-slug>[bot]`` — the
robotsix-project bot identity, without GitHub Actions.

The JWT/installation-token logic is delegated to the
``robotsix-github-auth`` library.  ``mint_installation_token`` is the
network/JWT seam (monkeypatched in tests, so pyjwt/httpx aren't needed
for the token-auth path or the suite).

Per-repo installations: when a ``RepoConfig`` with ``forge_remote_url``
is provided, the target owner/repo is derived from that repo's remote
instead of the global ``settings.forge_remote_url``.  This lets
different repos under the same (or different) GitHub Apps mint
installation tokens for their respective remotes.
"""

from __future__ import annotations

import logging
import time

import robotsix_github_auth as rga

from ..config import RepoConfig, Settings, get_secrets
from .github import _parse_owner_repo  # lazy: avoid import cycle

logger = logging.getLogger(__name__)


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

    Clears the library-level token cache so the next ``github_token()``
    call mints a fresh token from the GitHub API.  Safe to call when no
    entry exists.
    """
    remote_url = _resolve_remote_url(settings, repo_config)
    app_id = get_secrets().github_app_id or ""
    rga.clear_token_cache()
    logger.debug("invalidate_github_token app_id=%s remote_url=%s", app_id, remote_url)


def classify_token_error(exc: BaseException) -> str:
    """Classify an exception from token resolution as ``"transient"`` or
    ``"permanent"``.

    Transient errors (network blips, GitHub API degradation) are safe to
    retry — the token *can* be obtained once the remote is reachable again.
    Permanent errors (missing config, invalid key, App not installed) will
    never resolve on their own.

    Callers in the forge layer use this to decide whether a merge/push/PR
    operation that failed during auth should be marked ``retryable``, so
    the merge stage re-polls rather than blocking the ticket.
    """
    import httpx

    msg = str(exc)

    # -- permanent: configuration is missing or structurally wrong ---------
    if isinstance(exc, RuntimeError) and any(
        phrase in msg
        for phrase in (
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "FORGE_TOKEN",
            "FORGE_AUTH",
        )
    ):
        return "permanent"
    if isinstance(exc, rga.TokenMintError):
        return "permanent"
    # jwt / crypto errors: the key is present but invalid
    msg_lower = msg.lower()
    for jwt_module in ("jwt", "cryptography", "pyjwt"):
        if jwt_module in msg_lower:
            return "permanent"

    # -- transient: the remote is temporarily unreachable ------------------
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code >= 500:
            return "transient"
        # 4xx other than 401 (which _ApiClient._do already retries once)
        # is a permanent request error — the payload won't change on retry.
        return "permanent"
    if isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.RemoteProtocolError,
            httpx.NetworkError,
        ),
    ):
        return "transient"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        # OSError covers file-read failures on the private-key path
        # (e.g. NFS mount hiccup), DNS resolution failures, etc.
        return "transient"

    # Default conservative: treat unknown errors as permanent so a
    # truly broken config doesn't livelock.
    return "permanent"


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


def github_push_token(settings: Settings, repo_config: RepoConfig | None = None) -> str:
    """Return a token scoped for git push operations.

    Resolution order (PAT mode, ``forge_auth != "app"``):

    1. ``SANDBOX_PUSH_TOKEN`` from secrets — a dedicated push-bridge
       credential, isolated from the general forge token.  When set,
       a broken push token only blocks pushes, not PR creation or API
       calls.
    2. ``FORGE_TOKEN`` — the general forge PAT (fallback).

    When ``forge_auth == "app"``, delegates to the library's
    ``github_push_token`` with the owner/repo derived from the remote
    URL, scoped to ``contents: write`` and ``workflows: write``.
    """
    if settings.forge_auth != "app":
        push_token = get_secrets().sandbox_push_token
        if push_token:
            logger.debug("github_push_token: using dedicated sandbox_push_token")
            return push_token
        return github_token(settings, repo_config)

    if not get_secrets().github_app_id or not (
        get_secrets().github_app_private_key
        or get_secrets().github_app_private_key_path
    ):
        raise RuntimeError(
            "FORGE_AUTH=app needs GITHUB_APP_ID and GITHUB_APP_PRIVATE_KEY[_PATH]"
        )

    remote_url = _resolve_remote_url(settings, repo_config)
    owner, repo = _parse_owner_repo(remote_url)

    return rga.github_push_token(
        pat=get_secrets().forge_token or None,
        app_id=get_secrets().github_app_id,
        private_key=_private_key(),
        owner=owner,
        repo=repo,
        scopes={"contents": "write", "workflows": "write"},
        auth_mode="app",
    )


def github_token(settings: Settings, repo_config: RepoConfig | None = None) -> str:
    """Return a forge auth token: either a static FORGE_TOKEN from secrets
    or a short-lived GitHub App installation token.

    Delegates to the ``robotsix-github-auth`` library for token resolution
    and caching.
    """
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
    owner, repo = _parse_owner_repo(remote_url)

    return rga.github_token(
        pat=get_secrets().forge_token or None,
        app_id=get_secrets().github_app_id,
        private_key=_private_key(),
        owner=owner,
        repo=repo,
        auth_mode="app",
    )


def _private_key() -> str:
    """Read the GitHub App private key from the secrets path or value."""
    if get_secrets().github_app_private_key_path:
        with open(get_secrets().github_app_private_key_path, encoding="utf-8") as f:
            return f.read()
    key = get_secrets().github_app_private_key or ""
    # allow a single-line env value with literal "\n"
    return key.replace("\\n", "\n")