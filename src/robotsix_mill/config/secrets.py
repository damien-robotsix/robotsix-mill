"""The :class:`Secrets` model and its cached accessors.

Secrets are never merged into ``Settings`` — they are kept in a
separate model.  All secret fields use :class:`pydantic.SecretStr`
so the values are masked in ``repr`` / ``str`` and the JSON Schema
marks them as ``writeOnly`` password fields for the deploy UI.

The cached singleton is maintained by :func:`.mill_config.load_mill_config`;
:func:`get_secrets` returns the ``secrets`` field from the cached
``MillConfig``.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator

logger = logging.getLogger(__name__)


class Secrets(BaseModel):
    """API keys, tokens, and credentials for external services
    (OpenRouter, forge, Langfuse, ntfy, etc.).
    """

    openrouter_api_key: SecretStr | None = Field(
        default=None,
        description="API key for OpenRouter model access (https://openrouter.ai/keys). Required for LLM inference.",
    )
    forge_token: SecretStr | None = Field(
        default=None,
        description="Personal access token (PAT) for the forge (GitHub/GitLab). Used for PR creation, branch push, and merge operations.",
    )
    # A classic/fine-grained PAT used ONLY for repo creation (POST
    # /user/repos or /orgs/.../repos). GitHub App installation tokens
    # cannot create repositories under a personal account ("Resource not
    # accessible by integration"), so this PAT — with repo-creation
    # rights — is preferred for that one call while everyday push/PR
    # keeps using the App token. Falls back to the normal token if unset.
    forge_repo_create_token: SecretStr | None = Field(
        default=None,
        description="A PAT used ONLY for repository creation (POST /user/repos). Falls back to forge_token if unset. GitHub App tokens cannot create repos — use a classic PAT with repo-creation scope.",
    )
    github_app_id: str | None = Field(
        default=None,
        description="GitHub App ID for App-based authentication. Required when FORGE_AUTH=app.",
    )
    github_app_private_key: SecretStr | None = Field(
        default=None,
        description="GitHub App private key (PEM string). Alternative to github_app_private_key_path.",
    )
    github_app_private_key_path: str | None = Field(
        default=None,
        description="Path to the GitHub App private key PEM file. Alternative to github_app_private_key.",
    )
    langfuse_public_key: SecretStr | None = Field(
        default=None,
        description="Langfuse public key for LLM observability tracing (https://cloud.langfuse.com).",
    )
    langfuse_secret_key: SecretStr | None = Field(
        default=None,
        description="Langfuse secret key for LLM observability tracing.",
    )
    langfuse_base_url: str | None = Field(
        default=None,
        description="Langfuse instance base URL. Defaults to https://cloud.langfuse.com when unset.",
    )
    langfuse_project_id: str | None = Field(
        default=None,
        description="Langfuse project ID for trace attribution.",
    )
    langfuse_project_name: str | None = Field(
        default=None,
        description="Langfuse project name for trace attribution.",
    )
    openrouter_management_key: SecretStr | None = Field(
        default=None,
        description="OpenRouter management API key for credit-balance polling (https://openrouter.ai/keys).",
    )
    ntfy_url: str | None = Field(
        default=None,
        description="ntfy server URL for push notifications (https://ntfy.sh).",
    )
    ntf_token: SecretStr | None = Field(
        default=None,
        description="ntfy access token for authenticated push notifications.",
    )

    @model_validator(mode="before")
    @classmethod
    def _replace_sentinel_with_none(cls, data: Any) -> Any:
        """Replace the ``"SECRET"`` sentinel (used in ``config.example.json``)
        with ``None`` so the example file behaves like "no secret configured"."""
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and value == "SECRET":  # noqa: S105
                    data[key] = None
        return data

    def __repr__(self) -> str:
        field_names = list(type(self).model_fields.keys())
        inner = ", ".join(f"{name}='***'" for name in field_names)
        return f"Secrets({inner})"

    def model_dump(self, *, redact: bool = True, **kwargs: Any) -> dict[str, Any]:
        """Dump fields to dict, redacting all values by default."""
        d: dict[str, Any] = super().model_dump(**kwargs)
        if redact:
            return {k: "***" for k in d}
        return d

    def __getattribute__(self, name: str) -> Any:
        # Log every "public" field access at DEBUG level.
        # We must bypass our own override for private/special attrs
        # and for the fields dict itself to avoid infinite recursion.
        if not name.startswith("_") and name not in (
            "model_fields",
            "model_config",
            "model_dump",
            "__class__",
            "__dict__",
        ):
            fields = type(self).model_fields
            if name in fields:
                frame = inspect.currentframe()
                if frame is not None:
                    caller_frame = frame.f_back
                    if caller_frame is not None:
                        caller_module = caller_frame.f_globals.get(
                            "__name__", "unknown"
                        )
                    else:
                        caller_module = "unknown"
                else:
                    caller_module = "unknown"
                # Use a logger scoped to this module so tests can capture it
                _logger = logging.getLogger(__name__)
                _logger.debug("Secrets.%s accessed by %s", name, caller_module)
        try:
            value = super().__getattribute__(name)
        except AttributeError:
            # Pydantic v2 field lookup: model_computed_fields etc.
            return object.__getattribute__(self, name)
        # Transparently unwrap SecretStr so existing callers keep working
        # with plain strings without needing .get_secret_value().
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return value


def load_secrets(secrets_file: str | None = None) -> Secrets:
    """Load and return a :class:`Secrets` instance from the JSON config file.

    Delegates to :func:`~.mill_config.load_mill_config` which reads
    ``config/config.json`` (or ``ROBOTSIX_CONFIG_FILE``) via
    ``robotsix_config.load_config`` and returns the cached
    ``MillConfig.secrets`` field.

    When *secrets_file* is provided the cache is bypassed so a test
    that monkeypatches the file sees a fresh load.
    """
    from .mill_config import load_mill_config

    return load_mill_config(config_file=secrets_file).secrets


def get_secrets() -> Secrets:
    """Return a cached :class:`Secrets` singleton.

    When ``robotsix_mill.config._secrets`` is set (by test fixtures),
    returns that instance directly — this preserves the long-standing
    test pattern of assigning ``_cfg._secrets = Secrets(...)``.

    Otherwise loads from the JSON config file via
    :func:`~.mill_config.load_mill_config`.
    """
    import robotsix_mill.config as _pkg

    if _pkg._secrets is not None:
        return _pkg._secrets
    from .mill_config import load_mill_config

    return load_mill_config().secrets


def _reset_secrets() -> None:
    """Clear the cached secrets and config singletons (for tests)."""
    import robotsix_mill.config as _pkg

    _pkg._secrets = None
    from .mill_config import _reset_config_cache

    _reset_config_cache()
