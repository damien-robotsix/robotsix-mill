"""Thin accessor for secrets stored as SecretStr fields on Settings.

Secrets are now first-class fields on :class:`~robotsix_mill.config.Settings`,
loaded via ``robotsix_config.load_config(Settings)``. This module provides a
backward-compatible :func:`get_secrets` that unwraps ``SecretStr`` → ``str | None``
so existing callers (``get_secrets().openrouter_api_key``, etc.) keep working.

The class-level ``model_fields`` and ``model_json_schema()`` exist for
backward-compat with ``check_config_sync.py`` and ``emit_config_schema.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Canonical list of every user-configurable secret field.
#  Must match the ``SecretStr`` fields on ``Settings``.
# ---------------------------------------------------------------------------

_SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "openrouter_api_key",
        "forge_token",
        "forge_repo_create_token",
        "sandbox_push_token",
        "github_app_id",
        "github_app_private_key",
        "github_app_private_key_path",
        "langfuse_public_key",
        "langfuse_secret_key",
        "langfuse_base_url",
        "langfuse_project_id",
        "langfuse_project_name",
        "openrouter_management_key",
        "ntfy_url",
        "ntfy_token",
    }
)

# Sentinel value for unset secrets in example configs.
_SECRET_SENTINEL = "SECRET"  # noqa: S105

# ---------------------------------------------------------------------------
#  Pseudo-pydantic model_fields + model_json_schema for backward-compat
# ---------------------------------------------------------------------------


def _make_dummy_field(name: str) -> SimpleNamespace:
    """Return a minimal object that quacks like a pydantic FieldInfo for *name*."""
    return SimpleNamespace(alias=None, default=None, annotation=str | None)


# Build once at import time.
_secret_field_info: dict[str, SimpleNamespace] = {
    name: _make_dummy_field(name) for name in sorted(_SECRET_FIELD_NAMES)
}


class _SecretsModelFields(dict[str, SimpleNamespace]):
    """A dict subclass that ``set(_SecretsModelFields)`` iterates keys.

    Used as the ``Secrets.model_fields`` class attribute so the
    ``set(Secrets.model_fields)`` call in ``check_config_sync.py`` works.
    """

    def __iter__(self) -> Any:
        return iter(_secret_field_info)

    def items(self) -> Any:
        return _secret_field_info.items()

    def keys(self) -> Any:
        return _secret_field_info.keys()

    def values(self) -> Any:
        return _secret_field_info.values()

    def __contains__(self, key: object) -> bool:
        return key in _secret_field_info

    def __getitem__(self, key: str) -> SimpleNamespace:
        return _secret_field_info[key]

    def __len__(self) -> int:
        return len(_secret_field_info)


# ---------------------------------------------------------------------------
#  JSON Schema (backward-compat with emit_config_schema.py)
# ---------------------------------------------------------------------------


def _build_secrets_schema() -> dict[str, Any]:
    """Build a JSON Schema dict for the secrets block.

    Mirrors what the old ``Secrets(BaseModel).model_json_schema()`` produced:
    a ``type: object`` with a ``properties`` dict where each field is
    ``str | None``.
    """
    properties: dict[str, Any] = {}
    for name in sorted(_SECRET_FIELD_NAMES):
        title = name.replace("_", " ").title()
        properties[name] = {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "description": "",
            "title": title,
        }
    return {
        "properties": properties,
        "title": "Secrets",
        "type": "object",
    }


# ---------------------------------------------------------------------------
#  Secrets class
# ---------------------------------------------------------------------------


class Secrets:
    """Backward-compatible accessor that unwraps ``SecretStr`` → ``str | None``.

    Construct with kwargs to override specific secrets::

        Secrets(openrouter_api_key="sk-...")

    Pass ``_secrets_file=<path>`` to load from a JSON file (keys read from
    the ``"secrets"`` block).  File values override defaults; kwargs
    override file values.  The ``"SECRET"`` sentinel is treated as unset.
    """

    # --- Class-level attributes for backward-compat -----------------------

    # ``check_config_sync.py`` does ``set(Secrets.model_fields)``.
    model_fields: dict[str, SimpleNamespace] = _SecretsModelFields()

    # A set of field names — convenient for ``_is_secret_field`` and callers.
    _secret_field_names: frozenset[str] = _SECRET_FIELD_NAMES

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        """Return a JSON Schema for the secrets block.

        Used by ``emit_config_schema.py`` and ``_check_advanced.py``.
        """
        return _build_secrets_schema()

    # --- Instance ------------------------------------------------

    def __init__(self, **data: Any):
        secrets_file = data.pop("_secrets_file", None)

        # 1. Overlay from explicit JSON file when given.
        if secrets_file:
            file_data = self._load_secrets_file(secrets_file)
            data = {**file_data, **data}

        # 2. Apply kwargs (dropping "SECRET" sentinels).
        for name, value in data.items():
            if (
                name in _SECRET_FIELD_NAMES
                and value != _SECRET_SENTINEL
                and value is not None
            ):
                object.__setattr__(self, f"_{name}", value)
            elif name in _SECRET_FIELD_NAMES:
                object.__setattr__(self, f"_{name}", None)

        # Ensure all fields have a backing store.
        for name in _SECRET_FIELD_NAMES:
            if not hasattr(self, f"_{name}"):
                object.__setattr__(self, f"_{name}", None)

    @staticmethod
    def _load_secrets_file(path: str) -> dict[str, Any]:
        """Read the ``secrets:`` block from a JSON file."""
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError, json.JSONDecodeError:
            return {}
        if not isinstance(raw, dict):
            return {}
        secrets_block = raw.get("secrets", {})
        return secrets_block if isinstance(secrets_block, dict) else {}

    # --- Property accessors (unwrapping SecretStr) -----------------------

    @property
    def openrouter_api_key(self) -> str | None:
        """Return the OpenRouter API key."""
        return self._openrouter_api_key  # type: ignore[no-any-return]

    @property
    def forge_token(self) -> str | None:
        """Return the forge authentication token."""
        return self._forge_token  # type: ignore[no-any-return]

    @property
    def forge_repo_create_token(self) -> str | None:
        """Return the forge repo-creation token."""
        return self._forge_repo_create_token  # type: ignore[no-any-return]

    @property
    def sandbox_push_token(self) -> str | None:
        """Return the sandbox git-push token."""
        return self._sandbox_push_token  # type: ignore[no-any-return]

    @property
    def github_app_id(self) -> str | None:
        """Return the GitHub App ID."""
        return self._github_app_id  # type: ignore[no-any-return]

    @property
    def github_app_private_key(self) -> str | None:
        """Return the GitHub App private key."""
        return self._github_app_private_key  # type: ignore[no-any-return]

    @property
    def github_app_private_key_path(self) -> str | None:
        """Return the GitHub App private key path."""
        return self._github_app_private_key_path  # type: ignore[no-any-return]

    @property
    def langfuse_public_key(self) -> str | None:
        """Return the Langfuse public key."""
        return self._langfuse_public_key  # type: ignore[no-any-return]

    @property
    def langfuse_secret_key(self) -> str | None:
        """Return the Langfuse secret key."""
        return self._langfuse_secret_key  # type: ignore[no-any-return]

    @property
    def langfuse_base_url(self) -> str | None:
        """Return the Langfuse base URL."""
        return self._langfuse_base_url  # type: ignore[no-any-return]

    @property
    def langfuse_project_id(self) -> str | None:
        """Return the Langfuse project ID."""
        return self._langfuse_project_id  # type: ignore[no-any-return]

    @property
    def langfuse_project_name(self) -> str | None:
        """Return the Langfuse project name."""
        return self._langfuse_project_name  # type: ignore[no-any-return]

    @property
    def openrouter_management_key(self) -> str | None:
        """Return the OpenRouter management API key."""
        return self._openrouter_management_key  # type: ignore[no-any-return]

    @property
    def ntfy_url(self) -> str | None:
        """Return the ntfy.sh topic URL."""
        return self._ntfy_url  # type: ignore[no-any-return]

    @property
    def ntfy_token(self) -> str | None:
        """Return the ntfy.sh bearer token."""
        return self._ntfy_token  # type: ignore[no-any-return]

    # --- Debug logging for field access ----------------------------------

    def __getattribute__(self, name: str) -> Any:
        # Let special names through without logging.
        if name.startswith("_") or name in (
            "model_fields",
            "model_dump",
            "model_json_schema",
            "_secret_field_names",
        ):
            return object.__getattribute__(self, name)

        # Log public field access so callers are observable.
        if name in _SECRET_FIELD_NAMES:
            import inspect

            caller = inspect.currentframe()
            if caller is not None:
                caller = caller.f_back
            caller_name = caller.f_globals.get("__name__", "?") if caller else "?"
            logger.debug("Secrets.%s accessed by %s", name, caller_name)

        return object.__getattribute__(self, name)

    # --- Redacted repr ---------------------------------------------------

    def __repr__(self) -> str:
        parts = ", ".join(f"{name}='***'" for name in sorted(_SECRET_FIELD_NAMES))
        return f"Secrets({parts})"

    # --- model_dump (backward-compat) ------------------------------------

    def model_dump(self, *, redact: bool = True, **kwargs: Any) -> dict[str, Any]:
        """Return a dict representation (redacted by default)."""
        if redact:
            return dict.fromkeys(sorted(_SECRET_FIELD_NAMES), "***")
        return {name: getattr(self, name) for name in sorted(_SECRET_FIELD_NAMES)}


# ---------------------------------------------------------------------------
#  Module-level accessors
# ---------------------------------------------------------------------------


def load_secrets(secrets_file: str | None = None) -> Secrets:
    """Load secrets.

    When *secrets_file* is given, reads the ``secrets:`` block from that
    JSON file.  Otherwise returns a ``Secrets`` backed by the live
    ``Settings`` (``config/config.json``).
    """
    if secrets_file:
        return Secrets(_secrets_file=secrets_file)
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
