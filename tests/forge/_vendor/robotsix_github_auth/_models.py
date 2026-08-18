"""Data models for the robotsix-github-auth library."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime


@dataclass
class InstallationToken:
    """A GitHub App installation access token.

    Attributes:
        token: The raw bearer token string.
        expires_at: The UTC timestamp at which the token expires.
        permissions: The effective permissions map attached to the token.
    """

    token: str
    expires_at: datetime
    permissions: dict[str, str] = field(default_factory=dict)

    @property
    def seconds_remaining(self) -> float:
        """Seconds until this token expires (may be negative if expired)."""
        return (self.expires_at - datetime.now(UTC)).total_seconds()

    def is_expired(self, margin_seconds: float = 0.0) -> bool:
        """True when the token is expired, optionally within *margin_seconds*."""
        return self.seconds_remaining < margin_seconds
