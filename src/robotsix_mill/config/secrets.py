"""Thin accessor for secrets stored as SecretStr fields on Settings.

Secrets are now first-class fields on :class:`~robotsix_mill.config.Settings`,
loaded via ``robotsix_config.load_config(Settings)``. This module provides a
backward-compatible :func:`get_secrets` that unwraps ``SecretStr`` → ``str | None``
so existing callers (``get_secrets().openrouter_api_key``, etc.) keep working.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_settings():
    """Return the resolved Settings, using the cached singleton when available."""
    from .settings import Settings, load_settings

    import robotsix_mill.config as _pkg

    cached = getattr(_pkg, "_settings", None)
    if cached is None:
        cached = load_settings()
        _pkg._settings = cached
    return cached


@dataclass(frozen=True)
class _SecretsView:
    """Read-only view that unwraps SecretStr → str | None."""
    _settings: Any  # Settings

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._settings, name, None)
        if value is not None and hasattr(value, "get_secret_value"):
            return value.get_secret_value()
        return value

    def __repr__(self) -> str:
        return "Secrets(***)"


# Backward-compatible Secrets class for tests that construct Secrets(**overrides).
# This is NOT the same as the old Secrets model — it wraps Settings.
class Secrets:
    """Backward-compatible wrapper that constructs a Settings internally.

    Tests do ``Secrets(openrouter_api_key="sk-...")`` — we build a minimal
    Settings from those kwargs and return a _SecretsView.
    """
    def __init__(self, **data: Any):
        from .settings import Settings

        # Build Settings from kwargs, using field defaults for everything else
        self._settings = Settings.model_validate(data)

    @property
    def openrouter_api_key(self) -> str | None:
        v = self._settings.openrouter_api_key
        return v.get_secret_value() if v else None

    @property
    def forge_token(self) -> str | None:
        v = self._settings.forge_token
        return v.get_secret_value() if v else None

    @property
    def forge_repo_create_token(self) -> str | None:
        v = self._settings.forge_repo_create_token
        return v.get_secret_value() if v else None

    @property
    def sandbox_push_token(self) -> str | None:
        v = self._settings.sandbox_push_token
        return v.get_secret_value() if v else None

    @property
    def github_app_id(self) -> str | None:
        v = self._settings.github_app_id
        return v.get_secret_value() if v else None

    @property
    def github_app_private_key(self) -> str | None:
        v = self._settings.github_app_private_key
        return v.get_secret_value() if v else None

    @property
    def github_app_private_key_path(self) -> str | None:
        v = self._settings.github_app_private_key_path
        return v.get_secret_value() if v else None

    @property
    def langfuse_public_key(self) -> str | None:
        v = self._settings.langfuse_public_key
        return v.get_secret_value() if v else None

    @property
    def langfuse_secret_key(self) -> str | None:
        v = self._settings.langfuse_secret_key
        return v.get_secret_value() if v else None

    @property
    def langfuse_base_url(self) -> str | None:
        v = self._settings.langfuse_base_url
        return v.get_secret_value() if v else None

    @property
    def langfuse_project_id(self) -> str | None:
        v = self._settings.langfuse_project_id
        return v.get_secret_value() if v else None

    @property
    def langfuse_project_name(self) -> str | None:
        v = self._settings.langfuse_project_name
        return v.get_secret_value() if v else None

    @property
    def openrouter_management_key(self) -> str | None:
        v = self._settings.openrouter_management_key
        return v.get_secret_value() if v else None

    @property
    def ntfy_url(self) -> str | None:
        v = self._settings.ntfy_url
        return v.get_secret_value() if v else None

    @property
    def ntfy_token(self) -> str | None:
        v = self._settings.ntfy_token
        return v.get_secret_value() if v else None

    def __repr__(self) -> str:
        return "Secrets(***)"

    def model_dump(self, *, redact: bool = True, **kwargs: Any) -> dict[str, Any]:
        if redact:
            return {k: "***" for k in self._settings.model_fields if self._is_secret_field(k)}
        return {k: getattr(self, k) for k in self._settings.model_fields if self._is_secret_field(k)}

    @staticmethod
    def _is_secret_field(name: str) -> bool:
        return name in {
            "openrouter_api_key", "forge_token", "forge_repo_create_token",
            "sandbox_push_token", "github_app_id", "github_app_private_key",
            "github_app_private_key_path", "langfuse_public_key",
            "langfuse_secret_key", "langfuse_base_url", "langfuse_project_id",
            "langfuse_project_name", "openrouter_management_key", "ntfy_url",
            "ntfy_token",
        }


def load_secrets(secrets_file: str | None = None) -> Secrets:
    """Load secrets from Settings. The *secrets_file* arg is ignored."""
    return Secrets()


def get_secrets() -> Secrets:
    """Return a cached Secrets wrapper, constructing it on first call."""
    import robotsix_mill.config as _pkg

    cached = _pkg._secrets
    if cached is None:
        cached = Secrets()
        _pkg._secrets = cached
    return cached


def _reset_secrets() -> None:
    """Clear the cached Secrets singleton (for tests)."""
    import robotsix_mill.config as _pkg

    _pkg._secrets = None
