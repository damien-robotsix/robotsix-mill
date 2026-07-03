"""Pydantic-settings JSON source for :class:`Settings`.

Split out of the former monolithic ``config.py`` module; see
``config/__init__.py`` for the package layout rationale.
"""

from __future__ import annotations

from typing import Any

from pydantic_settings import PydanticBaseSettingsSource


class JsonSettingsSource(PydanticBaseSettingsSource):
    """Pydantic-settings source that loads JSON config via the existing
    ``load_config()`` pipeline.

    Called at ``Settings()`` construction time (not import time), so
    test monkeypatching of ``_CONFIG_FILE`` / ``_EXAMPLE_FILE`` /
    ``MILL_CONFIG_FILE`` works reliably.

    Returns an alias-keyed ``{alias: value}`` dict (e.g.
    ``{"MILL_MAX_CONCURRENCY": 4}``), matching the convention used by
    ``EnvSettingsSource`` / ``DotEnvSettingsSource`` in
    pydantic-settings, so ``populate_by_name`` is not required.

    Only fields whose env-var alias appears in the JSON config output
    are included — all others fall through to subsequent (lower-priority)
    sources or Field defaults.
    """

    def get_field_value(self, field: Any, field_name: str) -> Any:
        # Not used — __call__ is overridden directly.
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        from .loader import load_config

        flat = load_config()
        result: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            alias: str | None = field_info.alias
            key = alias if alias is not None else field_name
            if key in flat:
                # Return alias-keyed dict so pydantic-settings recognises the
                # values — the framework passes source dicts directly as
                # ``super().__init__(**state)``, and pydantic only accepts
                # alias names (not Python field names) when
                # ``populate_by_name`` is False (the default).
                result[key] = flat[key]
        return result
