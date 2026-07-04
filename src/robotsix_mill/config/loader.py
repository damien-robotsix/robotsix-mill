"""Configuration loader for robotsix-mill.

The mill reads a SINGLE config file: ``config/config.json`` (gitignored)
when present, else the committed ``config/config.example.json`` template.
Loading is delegated to ``robotsix_config.load_config`` via
:class:`~.mill_config.MillConfig` — there is no hand-rolled JSON parsing.

The ``repos:`` block is post-processed by :func:`load_repos_json` (operator
entries from JSON) merged with the machine-owned overlay
(``<data_dir>/registered_repos.yaml``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
#  Exception
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised for config-loading failures — missing required files,
    JSON parse errors, etc."""

    pass


# ---------------------------------------------------------------------------
#  File paths
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path("config")
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_EXAMPLE_FILE = _CONFIG_DIR / "config.example.json"


def _resolve_config_path() -> Path:
    """Resolve the single config file to read.

    Honors ``ROBOTSIX_CONFIG_FILE`` env var; falls back to
    ``config/config.json`` if it exists, else the committed
    ``config/config.example.json`` template (hermetic fallback).
    """
    from robotsix_config import CONFIG_FILE_ENV

    env = os.environ.get(CONFIG_FILE_ENV)
    if env:
        return Path(env)
    return _CONFIG_FILE if _CONFIG_FILE.exists() else _EXAMPLE_FILE


def _load_repos_document(file_path: str | None = None) -> dict[str, object]:  # noqa: C901
    """Read and parse the repos configuration document.

    Merges operator repos from the JSON config (``repos:`` key) with
    machine-owned overlay entries from ``<data_dir>/registered_repos.yaml``.
    The operator entry wins on repo-id conflict. Returns the merged
    ``{"repos": {...}}`` mapping, or ``{}`` when nothing is configured —
    zero repos is valid.

    An explicit *file_path* arg or the ``MILL_REPOS_FILE`` env var overrides
    the config file and reads the given file directly (used by the test
    suite); an explicit ``""`` means "no repos".
    """
    # 1. Explicit override: arg > env var — reads the given file directly.
    if file_path is not None:
        path_str: str | None = file_path
    else:
        path_str = os.environ.get("MILL_REPOS_FILE")
    if path_str == "":
        return {}
    if path_str is not None:
        path = Path(path_str)
        if not path.exists():
            return {}
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    # 2. Operator repos from the JSON config (``repos:`` key).
    try:
        raw_path = _resolve_config_path()
        if raw_path.exists():
            raw_data = json.loads(raw_path.read_text(encoding="utf-8"))
            cfg_raw = raw_data if isinstance(raw_data, dict) else {}
        else:
            cfg_raw = {}
    except ConfigError:
        cfg_raw = {}

    has_operator_key = isinstance(cfg_raw, dict) and "repos" in cfg_raw
    operator_repos: dict[str, object] = (
        (cfg_raw.get("repos") or {}) if has_operator_key else {}
    )
    if not isinstance(operator_repos, dict):
        operator_repos = {}

    # 3. Machine-owned overlay: <data_dir>/registered_repos.yaml.
    #    data_dir is read from the settings model.
    from .settings import load_settings

    try:
        settings = load_settings()
        data_dir_str: str = str(settings.data_dir)
    except ConfigError:
        data_dir_str = ".data"

    overlay_path = Path(data_dir_str) / "registered_repos.yaml"
    overlay_repos: dict[str, object] = {}
    if overlay_path.exists():
        try:
            overlay_data = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
            raw_overlay = (
                (overlay_data.get("repos") or {})
                if isinstance(overlay_data, dict)
                else {}
            )
            if isinstance(raw_overlay, dict):
                overlay_repos = raw_overlay
        except yaml.YAMLError, OSError:
            pass  # corrupt overlay is tolerated; treat as empty

    # 4. Inject source marker so load_repos_config can set RepoConfig.source.
    for entry in overlay_repos.values():
        if isinstance(entry, dict):
            entry.setdefault("_mill_source", "auto")

    # 5. Merge: operator wins on repo-id conflict.
    if operator_repos or overlay_repos:
        merged = {**overlay_repos, **operator_repos}  # operator overwrites overlay
        return {"repos": merged}

    return {}


def load_repos_json(file_path: str | None = None) -> dict[str, object]:
    """Read the merged repos configuration (JSON config ``repos:`` key +
    ``<data_dir>/registered_repos.yaml``).

    Returns a dict keyed by repo ID with nested ``board_id`` and
    ``langfuse`` sub-dicts
    (e.g. ``{"my-repo": {"board_id": "...", "langfuse": {...}}, ...}``).

    Missing key/file → returns an empty dict (not an error — repos config
    is optional).

    Malformed JSON → raises ``ConfigError`` with the file path and
    parse error details.
    """
    data = _load_repos_document(file_path)
    if not data:
        return {}
    # Extract the ``repos`` key if present (standard format).
    if "repos" in data:
        repos_data = data["repos"]
        if not isinstance(repos_data, dict):
            raise ConfigError(
                f"Expected a mapping under the 'repos' key, "
                f"got {type(repos_data).__name__}"
            )
        return dict(repos_data)
    # Flat format (override files only): the document IS the repo mapping.
    # The sibling ``meta`` block is not a repo, so never surface it as one.
    return {k: v for k, v in data.items() if k != "meta"}
