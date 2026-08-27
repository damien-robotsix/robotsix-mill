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
    a test's ``ROBOTSIX_CONFIG_FILE`` monkeypatch — and
    a config file rewritten at runtime by ``PUT /config`` — are both picked up.

    The config's ``settings`` block is flat and alias-keyed (e.g.
    ``{"data_dir": ".data", "MILL_MAX_GLOBAL_CONCURRENCY": 12}``), matching the
    convention pydantic-settings' own env sources use.

    Only keys matching a model field (by alias or by name) are returned, so
    unrelated keys in the file can't trip ``extra="forbid"``.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Raise ``NotImplementedError`` — unused; ``__call__`` is overridden directly."""
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        """Return a flat dict of settings values keyed by field alias.

        Only keys matching a model field (by alias, validation_alias, or by
        name) are returned.  ``load_settings_block()`` already renames legacy
        UPPERCASE config keys to lowercase, so the primary lookup is by
        field name; the alias/validation_alias fallback catches env-var-style
        keys that slipped through (e.g. from tests or manual config edits).
        """
        from pydantic import AliasChoices

        from .loader import load_settings_block

        settings_data = load_settings_block()
        result: dict[str, Any] = {}

        # Build a reverse lookup: every known key variant (alias,
        # validation_alias choices, field_name) → canonical field_name.
        known: dict[str, str] = {}
        for fname, finfo in self.settings_cls.model_fields.items():
            known[fname] = fname
            if finfo.alias:
                known[finfo.alias] = fname
            va = finfo.validation_alias
            if isinstance(va, AliasChoices):
                for choice in va.choices:
                    known[str(choice)] = fname
            elif va is not None:
                known[str(va)] = fname

        for key, value in settings_data.items():
            canonical = known.get(key)
            if canonical is not None:
                # Later values win if the canonical field wasn't set yet,
                # or if the key IS the canonical name.
                result[canonical] = value

        return result
