"""Typed exceptions for the robotsix-github-auth library."""

from __future__ import annotations


class GithubAuthError(Exception):
    """Base exception for all GitHub Auth errors."""


class TokenMintError(GithubAuthError):
    """Raised when a token cannot be minted.

    This covers HTTP failures, JWT signing errors, and missing parameters.
    """


class ScopeError(GithubAuthError):
    """Raised when token permissions are insufficient for the requested operation."""

    def __init__(self, message: str, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing: list[str] = missing or []
