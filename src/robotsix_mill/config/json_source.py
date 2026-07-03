"""Pydantic-settings JSON source for :class:`Settings`.

Split out of the former monolithic ``config.py`` module; see
``config/__init__.py`` for the package layout rationale.
"""

from __future__ import annotations

from typing import Any

from pydantic_settings import PydanticBaseSettingsSource


class JsonSettingsSource(PydanticBaseSettingsSource):
    """Pydantic-settings source that loads JSON config via
    ``load_config()``.

    Called at ``Settings()`` construction time (not import time), so
    test monkeypatching of ``_CONFIG_FILE`` / ``_EXAMPLE_FILE`` /
    ``MILL_CONFIG_FILE`` works reliably.

    The JSON config's ``settings`` dict is already flat and alias-keyed
    (e.g. ``{"data_dir": ".data", "MILL_MAX_GLOBAL_CONCURRENCY": 12}``),
    matching the convention used by ``EnvSettingsSource`` /
    ``DotEnvSettingsSource`` in pydantic-settings, so
    ``populate_by_name`` is not required.

    Only fields whose env-var alias (or Python field name) appears in the
    JSON settings are included — all others fall through to subsequent
    (lower-priority) sources or Field defaults.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Not used — __call__ is overridden directly.
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        from .loader import load_config

        settings_data: dict[str, object] = load_config()
        result: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            alias: str | None = field_info.alias
            # Determine the alias-keyed key: use alias if present, else field name
            key = alias if alias is not None else field_name
            # The JSON stores values under either the alias or the field name
            if key in settings_data:
                result[key] = settings_data[key]
            elif field_name != key and field_name in settings_data:
                # Field name exists in JSON but alias is different; promote to alias
                result[key] = settings_data[field_name]
        return result
