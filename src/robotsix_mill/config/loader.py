"""Minimal config loader shim — all loading is now via robotsix_config."""

from __future__ import annotations


class ConfigError(Exception):
    """Raised for config-loading failures."""
    pass


def load_repos_yaml(file_path: str | None = None) -> dict[str, object]:
    """Legacy compat: always returns empty dict. Config is now loaded via Settings.repos."""
    return {}
