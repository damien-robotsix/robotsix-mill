"""Pydantic-settings JSON source for :class:`Settings`.

Restored after the config-standard cutover (#2525) removed it: that commit
replaced the source with a ``load_settings()`` helper and never wired it to a
caller, so every bare ``Settings()`` — which is how essentially the whole
codebase reads config — silently returned model defaults.
"""

from __future__ import annotations

from typing import Any

from pydantic_settings import PydanticBaseSettingsSource


class JsonSettingsSource(PydanticBaseSettingsSource):
    """Pydantic-settings source that reads the main JSON config file.

    Resolved at ``Settings()`` construction time rather than import time, so
    a test's ``MILL_CONFIG_FILE`` / ``ROBOTSIX_CONFIG_FILE`` monkeypatch — and
    a config file rewritten at runtime by ``PUT /config`` — are both picked up.

    The config's ``settings`` block is flat and alias-keyed (e.g.
    ``{"data_dir": ".data", "MILL_MAX_GLOBAL_CONCURRENCY": 12}``), matching the
    convention pydantic-settings' own env sources use.

    Only keys matching a model field (by alias or by name) are returned, so
    unrelated keys in the file can't trip ``extra="forbid"``.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        # Not used — __call__ is overridden directly.
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        from .loader import load_settings_block

        settings_data = load_settings_block()
        result: dict[str, Any] = {}
        for field_name, field_info in self.settings_cls.model_fields.items():
            key = field_info.alias if field_info.alias is not None else field_name
            if key in settings_data:
                result[key] = settings_data[key]
            elif field_name != key and field_name in settings_data:
                # Present under the field name; promote to the alias so
                # populate_by_name isn't required.
                result[key] = settings_data[field_name]
        return result
