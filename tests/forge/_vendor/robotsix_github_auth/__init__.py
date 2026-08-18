"""robotsix-github-auth — Fleet-wide GitHub App token minting."""

from __future__ import annotations

from robotsix_github_auth._auth import github_push_token, github_token, mint_installation_token
from robotsix_github_auth._cache import _token_cache
from robotsix_github_auth._exceptions import GithubAuthError, ScopeError, TokenMintError
from robotsix_github_auth._models import InstallationToken
from robotsix_github_auth._scopes import validate_scopes


def invalidate_token_cache(installation_id: str) -> None:
    """Remove all cached tokens for *installation_id*."""
    _token_cache.invalidate(installation_id)


def clear_token_cache() -> None:
    """Remove every cached token."""
    _token_cache.clear()


__all__ = [
    "GithubAuthError",
    "InstallationToken",
    "ScopeError",
    "TokenMintError",
    "clear_token_cache",
    "github_push_token",
    "github_token",
    "invalidate_token_cache",
    "mint_installation_token",
    "validate_scopes",
]
