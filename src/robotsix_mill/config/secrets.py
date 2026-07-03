"""The :class:`Secrets` model and its cached accessors.

Split out of the former monolithic ``config.py``. The cached
``_secrets`` singleton lives in ``config/__init__.py`` so test fixtures
that poke ``robotsix_mill.config._secrets`` are observed by the
accessors here (which read the package attribute at call time).
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Secrets(BaseModel):
    """Secrets loaded from the ``secrets:`` block of the single mill
    config file (``config/config.json``, else ``config/config.example.json``).

    Never merged into ``Settings`` — secrets are kept in a separate
    model with redacted ``repr`` / ``model_dump`` and debug-logged
    attribute access.  A value equal to the literal ``SECRET`` sentinel
    (used throughout ``config.example.json``) is treated as unset, so the
    field falls back to its ``None`` default.
    """

    openrouter_api_key: str | None = None
    forge_token: str | None = None
    # A classic/fine-grained PAT used ONLY for repo creation (POST
    # /user/repos or /orgs/.../repos). GitHub App installation tokens
    # cannot create repositories under a personal account ("Resource not
    # accessible by integration"), so this PAT — with repo-creation
    # rights — is preferred for that one call while everyday push/PR
    # keeps using the App token. Falls back to the normal token if unset.
    forge_repo_create_token: str | None = None
    github_app_id: str | None = None
    github_app_private_key: str | None = None
    github_app_private_key_path: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_base_url: str | None = None
    langfuse_project_id: str | None = None
    langfuse_project_name: str | None = None
    openrouter_management_key: str | None = None
    ntfy_url: str | None = None
    ntfy_token: str | None = None

    def __init__(self, _secrets_file: str | None = None, **data: Any) -> None:
        """Construct a ``Secrets`` instance.

        If ``_secrets_file`` is provided it is used as the JSON source;
        otherwise ``MILL_SECRETS_FILE`` is consulted, falling back to the
        single mill config file (``config/config.json``, else
        ``config/config.example.json``).  Its ``secrets:`` block is passed
        as field defaults, which explicit ``**data`` kwargs can override.
        """
        from .loader import load_secrets as _load_secrets

        file_path: str | None = _secrets_file
        if file_path is None:
            import os

            file_path = os.environ.get("MILL_SECRETS_FILE")

        secrets_data = _load_secrets(file_path)
        merged = {**secrets_data, **data}
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
