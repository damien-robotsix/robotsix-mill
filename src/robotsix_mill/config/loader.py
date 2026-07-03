"""JSON configuration loader for robotsix-mill.

The mill reads a SINGLE config file: ``config/config.json`` (gitignored)
when present, else the committed ``config/config.example.json`` template
(so CI / tests without a ``config.json`` still get the committed
defaults).  The file holds every non-secret knob plus a top-level
``secrets:`` block; :func:`load_config` returns the non-secret part
(for the ``Settings`` model) and :func:`load_secrets_json` returns the
``secrets:`` sub-map (for the ``Secrets`` model).

The JSON ``settings`` keys are flat Pydantic field aliases (e.g.
``"MILL_MAX_GLOBAL_CONCURRENCY"``) — no nested-YAML-to-alias translation
layer is needed.
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
#  JSON loading
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path("config")
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_EXAMPLE_FILE = _CONFIG_DIR / "config.example.json"

# Literal placeholder used for every secret in ``config.example.json``.
# A secret leaf equal to this is treated as UNSET (the field falls back
# to its ``None`` default), so example / CI runs behave like "no secret
# configured".
_SECRET_SENTINEL = "SECRET"  # noqa: S105 — sentinel, not a real credential


def _resolve_config_path(config_file: str | None) -> Path:
    """Resolve the single config file to read.

    Precedence: explicit *config_file* arg > ``MILL_CONFIG_FILE`` env >
    default resolution (``config/config.json`` if it exists, else the
    committed ``config/config.example.json`` template).  An explicit
    empty string ``""`` means "use the committed example" — the hermetic
    choice used by the test suite (its secrets are all-``SECRET`` → unset).
    """
    if config_file is not None:
        explicit: str | None = config_file
    else:
        explicit = os.environ.get("MILL_CONFIG_FILE")

    if explicit:  # non-empty explicit path
        return Path(explicit)
    if explicit == "":  # explicit empty → committed example (hermetic)
        return _EXAMPLE_FILE
    # explicit is None → default resolution.
    return _CONFIG_FILE if _CONFIG_FILE.exists() else _EXAMPLE_FILE


def load_config(
    config_file: str | None = None, skip_local: bool = False
) -> dict[str, object]:
    """Load the single mill config file and return its non-secret part.

    Reads ``config/config.json`` when present, else the committed
    ``config/config.example.json`` (overridable via the *config_file* arg
    or the ``MILL_CONFIG_FILE`` env var; ``""`` forces the committed
    example).  The top-level ``secrets:`` block is stripped — it is
    consumed separately by :func:`load_secrets_json` / the ``Secrets``
    model, never merged into ``Settings``.

    Returns a flat dict keyed by Settings field aliases
    (e.g. ``{"data_dir": ".data", "api_port": 8077, ...}``).

    *skip_local* is accepted for backward compatibility and ignored —
    there is no longer a separate local overlay layer.

    Raises ``ConfigError`` if the resolved file is missing or contains
    malformed JSON.
    """
    del skip_local  # no-op: single-file model has no separate overlay
    path = _resolve_config_path(config_file)
    if not path.exists():
        raise ConfigError(
            f"Required config file not found: {path}. Copy "
            "config/config.example.json to config/config.json, or point "
            "MILL_CONFIG_FILE at a config file."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
    result = dict(data) if isinstance(data, dict) else {}
    result.pop("secrets", None)
    # Return just the ``settings`` sub-dict when present; fall back to
    # top-level (backward compat with flat top-level files).
    if "settings" in result:
        settings = result["settings"]
        if isinstance(settings, dict):
            return dict(settings)
    # Top-level-only format (no ``settings`` wrapper): return everything
    # except repos (which is consumed separately).
    result.pop("repos", None)
    return result


def load_secrets_json(secrets_file: str | None = None) -> dict[str, object]:
    """Return the ``secrets:`` sub-map of the single mill config file.

    Path resolution mirrors :func:`load_config`: explicit
    *secrets_file* arg > ``MILL_SECRETS_FILE`` env > default
    (``config/config.json`` if present, else ``config/config.example.json``).
    An explicit empty string ``""`` forces the committed example (used by
    the test suite).

    Returns a flat dict keyed by the secret field names
    (e.g. ``{"openrouter_api_key": "sk-...", ...}``).  Blank values and
    leaves equal to the ``SECRET`` sentinel are dropped so unset secrets
    fall back to the ``Secrets`` model's ``None`` defaults.

    Missing file → empty dict (not an error — secrets are optional for
    CI / mocked tests).  Malformed JSON → ``ConfigError``.
    """
    if secrets_file is not None:
        explicit: str | None = secrets_file
    else:
        explicit = os.environ.get("MILL_SECRETS_FILE")

    if explicit:
        path = Path(explicit)
    elif explicit == "":
        path = _EXAMPLE_FILE
    else:  # None → default resolution
        path = _CONFIG_FILE if _CONFIG_FILE.exists() else _EXAMPLE_FILE

    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc

    raw = data.get("secrets", {}) if isinstance(data, dict) else {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: value
        for key, value in raw.items()
        if not (
            value is None
            or (isinstance(value, str) and value.strip() in ("", _SECRET_SENTINEL))
        )
    }


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
        # Re-read the raw file to get repos
        raw_path = _resolve_config_path(None)
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
    #    data_dir is read from the settings to stay consistent without
    #    a circular import.
    try:
        settings = load_config()
        data_dir_str: str = str(settings.get("data_dir", ".data"))
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


# ---------------------------------------------------------------------------
#  Backward-compatible aliases
# ---------------------------------------------------------------------------

# These names were the public API before the JSON migration. Keep them
# as aliases so any remaining callers don't break immediately — they
# can be cleaned up in a follow-up.

load_yaml_config = load_config
load_secrets_yaml = load_secrets_json
load_repos_yaml = load_repos_json
