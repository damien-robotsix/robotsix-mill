"""The :class:`Secrets` model and its cached accessors.

Secrets are never merged into ``Settings`` — they are kept in a
separate model with redacted ``repr`` / ``model_dump`` and
debug-logged attribute access, so they cannot leak through logs,
trace output, or accidental serialization.

The cached ``_secrets`` singleton lives in ``config/__init__.py`` so
test fixtures that poke ``robotsix_mill.config._secrets`` are
observed by the accessors here (which read the package attribute at
call time).

Secrets are loaded from the ``secrets:`` block of the single mill
config file (``config/config.json``, else ``config/config.example.json``).
A value equal to the literal ``SECRET`` sentinel (used throughout
``config.example.json``) is treated as unset, so the field falls back
to its ``None`` default.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Secrets(BaseModel):
    """API keys, tokens, and credentials for external services
    (OpenRouter, forge, Langfuse, ntfy, etc.).
    """

    openrouter_api_key: str | None = Field(
        default=None,
        description="API key for OpenRouter model access (https://openrouter.ai/keys). Required for LLM inference.",
    )
    forge_token: str | None = Field(
        default=None,
        description="Personal access token (PAT) for the forge (GitHub/GitLab). Used for PR creation, branch push, and merge operations.",
    )
    # A classic/fine-grained PAT used ONLY for repo creation (POST
    # /user/repos or /orgs/.../repos). GitHub App installation tokens
    # cannot create repositories under a personal account ("Resource not
    # accessible by integration"), so this PAT — with repo-creation
    # rights — is preferred for that one call while everyday push/PR
    # keeps using the App token. Falls back to the normal token if unset.
    forge_repo_create_token: str | None = Field(
        default=None,
        description="A PAT used ONLY for repository creation (POST /user/repos). Falls back to forge_token if unset. GitHub App tokens cannot create repos — use a classic PAT with repo-creation scope.",
    )
    github_app_id: str | None = Field(
        default=None,
        description="GitHub App ID for App-based authentication. Required when FORGE_AUTH=app.",
    )
    github_app_private_key: str | None = Field(
        default=None,
        description="GitHub App private key (PEM string). Alternative to github_app_private_key_path.",
    )
    github_app_private_key_path: str | None = Field(
        default=None,
        description="Path to the GitHub App private key PEM file. Alternative to github_app_private_key.",
    )
    langfuse_public_key: str | None = Field(
        default=None,
        description="Langfuse public key for LLM observability tracing (https://cloud.langfuse.com).",
    )
    langfuse_secret_key: str | None = Field(
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
    openrouter_management_key: str | None = Field(
        default=None,
        description="OpenRouter management API key for credit-balance polling (https://openrouter.ai/keys).",
    )
    ntfy_url: str | None = Field(
        default=None,
        description="ntfy server URL for push notifications (https://ntfy.sh).",
    )
    ntfy_token: str | None = Field(
        default=None,
        description="ntfy access token for authenticated push notifications.",
    )

    def __init__(self, _secrets_file: str | None = None, **data: Any) -> None:
        """Construct a ``Secrets`` instance.

        If ``_secrets_file`` is provided it is used as the JSON source;
        otherwise ``MILL_SECRETS_FILE`` is consulted, falling back to the
        single mill config file (``config/config.json``, else
        ``config/config.example.json``).  Its ``secrets:`` block is passed
        as field defaults, which explicit ``**data`` kwargs can override.
        """
        from .loader import load_secrets_json

        file_path: str | None = _secrets_file
        if file_path is None:
            import os

            file_path = os.environ.get("MILL_SECRETS_FILE")

        yaml_data = load_secrets_json(file_path)
        merged = {**yaml_data, **data}
        super().__init__(**merged)

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
            return super().__getattribute__(name)
        except AttributeError:
            # Pydantic v2 field lookup: model_computed_fields etc.
            return object.__getattribute__(self, name)


def load_secrets(secrets_file: str | None = None) -> Secrets:
    """Load and return a :class:`Secrets` instance from JSON.

    If *secrets_file* is provided it is used as the JSON source;
    otherwise ``MILL_SECRETS_FILE`` is consulted, falling back to the
    single mill config file (``config/config.json``, else
    ``config/config.example.json``).
    """
    return Secrets(_secrets_file=secrets_file)


def get_secrets() -> Secrets:
    """Return a cached :class:`Secrets` singleton, constructing it on first call."""
    import robotsix_mill.config as _pkg

    cached = _pkg._secrets
    if cached is None:
        cached = Secrets()
        _pkg._secrets = cached
    return cached


def _reset_secrets() -> None:
    """Clear the cached :class:`Secrets` singleton (for tests)."""
    import robotsix_mill.config as _pkg

    _pkg._secrets = None
