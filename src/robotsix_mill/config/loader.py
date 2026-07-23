"""Minimal config loader shim — all loading is now via robotsix_config."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

# Sentinel to distinguish "env var absent" from "env var empty".
_MILL_REPOS_FILE_UNSET = object()


class ConfigError(Exception):
    """Raised for config-loading failures."""
    pass


def _mill_repos_file() -> str | None | object:
    """Return the ``MILL_REPOS_FILE`` env var, or sentinel when absent."""
    return os.environ.get("MILL_REPOS_FILE", _MILL_REPOS_FILE_UNSET)


def _resolve_main_config_path() -> Path | None:
    """Resolve the main config file path.

    Checks ``MILL_CONFIG_FILE`` (used by tests) first, then
    ``ROBOTSIX_CONFIG_FILE`` (used by robotsix_config), then the default
    ``config/config.json``.
    """
    for env_var in ("MILL_CONFIG_FILE", "ROBOTSIX_CONFIG_FILE"):
        env_path = os.environ.get(env_var)
        if env_path:
            return Path(env_path)
    default = Path("config/config.json")
    if default.exists():
        return default
    return None


def _load_file(target: Path) -> dict[str, Any]:
    """Load a YAML or JSON file, returning a dict (or {} on error)."""
    try:
        raw_text = target.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}

    # Try JSON first, then YAML.
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError:
            return {}

    return data if isinstance(data, dict) else {}


def _resolve_data_dir() -> Path:
    """Resolve ``data_dir`` from the main config when available, else ``.data``."""
    main_path = _resolve_main_config_path()
    if main_path is not None:
        main_data = _load_file(main_path)
        settings_block = main_data.get("settings", {})
        if isinstance(settings_block, dict):
            dd = settings_block.get("data_dir")
            if isinstance(dd, str) and dd:
                return Path(dd)
    return Path(".data")


def load_repos_yaml(file_path: str | None = None) -> dict[str, object]:
    """Load the ``repos:`` block.

    Priority:
    1. If *file_path* is given, read that file (YAML or JSON).
    2. If ``MILL_REPOS_FILE`` env var is set to a non-empty path, read that file.
    3. If ``MILL_REPOS_FILE`` is explicitly empty, return ``{}`` (no-op mode).
    4. Otherwise, read from the main ``config/config.json`` and merge in the
       machine-owned overlay (``<data_dir>/registered_repos.yaml``).

    Returns the raw ``repos`` mapping (or ``{}`` on any error / missing file).
    """
    # 1. Explicit file_path parameter wins.
    if file_path is not None:
        data = _load_file(Path(file_path))
        return data.get("repos", {}) if isinstance(data.get("repos"), dict) else {}

    # 2. MILL_REPOS_FILE env var.
    mill_repos = _mill_repos_file()
    if mill_repos is not _MILL_REPOS_FILE_UNSET:
        # Explicitly set — empty → no-op, else load the file.
        if mill_repos == "" or mill_repos is None:
            return {}
        data = _load_file(Path(str(mill_repos)))
        return data.get("repos", {}) if isinstance(data.get("repos"), dict) else {}

    # 3. Default: load main config + overlay merge.
    main_path = _resolve_main_config_path()
    main_repos: dict[str, Any] = {}
    if main_path is not None:
        main_data = _load_file(main_path)
        main_repos = main_data.get("repos", {}) if isinstance(main_data.get("repos"), dict) else {}

    # Merge overlay (machine-owned auto-registered repos).
    overlay_path = _resolve_data_dir() / "registered_repos.yaml"
    overlay_data = _load_file(overlay_path)
    overlay_repos: dict[str, Any] = (
        overlay_data.get("repos", {}) if isinstance(overlay_data.get("repos"), dict) else {}
    )

    # Merge: overlay first, then operator (operator wins on conflict).
    merged = {**overlay_repos, **main_repos}
    return merged
