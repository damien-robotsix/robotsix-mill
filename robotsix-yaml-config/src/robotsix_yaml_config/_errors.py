"""Error types for the YAML configuration cascade."""

from __future__ import annotations


class YamlConfigError(Exception):
    """Raised for YAML-config cascade failures.

    Covers missing required files, YAML parse errors, and non-dict
    top-level mappings.
    """

    pass
