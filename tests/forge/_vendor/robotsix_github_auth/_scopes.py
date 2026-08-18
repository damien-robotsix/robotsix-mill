"""Scope validation helpers for GitHub App permissions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from robotsix_github_auth._exceptions import ScopeError

# Permission levels in ascending order: read < write < admin
_LEVELS: dict[str, int] = {"read": 0, "write": 1, "admin": 2}


def validate_scopes(
    token_permissions: Mapping[str, Any],
    required: Mapping[str, str],
) -> None:
    """Validate that *token_permissions* satisfy *required* permissions.

    Args:
        token_permissions: The ``permissions`` dict from an installation token.
        required: A mapping of ``{permission_name: minimum_level}`` where
            *minimum_level* is one of ``"read"``, ``"write"``, or ``"admin"``.

    Raises:
        ValueError: When a required minimum level string is not one of
            ``"read"``, ``"write"``, or ``"admin"`` (a caller configuration error).
        ScopeError: When a required permission is missing or its level is
            lower than the required minimum.
    """
    missing: list[str] = []

    for scope, min_level_str in required.items():
        current = token_permissions.get(scope)
        if current is None:
            missing.append(scope)
            continue

        min_level = _LEVELS.get(min_level_str)
        if min_level is None:
            raise ValueError(
                f"Unknown required permission level '{min_level_str}' "
                f"for '{scope}' (expected one of: read, write, admin)"
            )

        current_level = _LEVELS.get(str(current))
        if current_level is None or current_level < min_level:
            missing.append(f"{scope} (has {current}, needs {min_level_str})")

    if missing:
        raise ScopeError(
            f"Insufficient permissions: {', '.join(sorted(missing))}",
            missing=sorted(missing),
        )
