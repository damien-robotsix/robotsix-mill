"""Overlay environment variables onto a flat config dict."""

from __future__ import annotations

import os
from typing import Any

# Case-insensitive truthy/falsy spellings for bool coercion.  A raw
# ``bool(value)`` is WRONG because ``bool("false")`` is ``True``.
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


def _coerce(value: str, hint: type) -> Any:
    """Coerce the string env *value* to the type given by *hint*."""
    if hint is bool:
        lowered = value.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
        # Fall back to truthy parse for unrecognised spellings.
        return bool(lowered)
    if hint is int:
        return int(value)
    if hint is float:
        return float(value)
    # ``str`` (and any unhandled hint) → value unchanged.
    return value


def overlay_env_vars(
    config: dict[str, Any],
    prefix: str,
    type_hints: dict[str, type] | None = None,
) -> dict[str, Any]:
    """Overlay ``{PREFIX}_{KEY.upper()}`` env vars onto *config*.

    For each key already present in *config*, look up the env var named
    ``f"{prefix}_{key.upper()}"``.  If it is set, coerce its string value
    using ``type_hints.get(key)`` (defaulting to ``str`` when absent or
    when *type_hints* is ``None``) and overwrite ``config[key]``.  Keys
    that are not already in *config* are never added.  Mutates and
    returns *config*.
    """
    hints = type_hints or {}
    for key in config:
        env_name = f"{prefix}_{key.upper()}"
        if env_name in os.environ:
            hint = hints.get(key, str)
            config[key] = _coerce(os.environ[env_name], hint)
    return config
