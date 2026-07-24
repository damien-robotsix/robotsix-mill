"""Component config HTTP service — GET /config, PUT /config,
GET /config/versions, POST /config/rollback.

Implements the config-ownership standard (robotsix-standards) for
mill's own component configuration surface.  Reads from and writes to
the single ``config/config.json`` (located via ``MILL_CONFIG_FILE`` env
var), maintains a version history in
``<data_dir>/config_versions.jsonl``, and masks secret values on read.

Secret fields are identified from the :class:`~robotsix_mill.config.Secrets`
model — they are env-injected by the deploy plane per the ticket spec,
never stored in the component config file, and never accepted via
``PUT /config``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from ..config import Settings, Secrets

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
#  Secret field identification
# ---------------------------------------------------------------------------

# Secret-key set derived from the Secrets model's field names, mapped to
# their uppercase alias form when that alias is used in the JSON config.
# The Secrets model field names (snake_case) are the canonical names;
# some Settings fields reference the same secret under a different alias.
_SECRET_FIELD_NAMES: frozenset[str] = frozenset(Secrets.model_fields.keys())

# Aliases used in the JSON config ``settings`` block that correspond to
# secrets (e.g. ``OPENROUTER_API_KEY`` is the Settings alias for
# ``openrouter_api_key``, which is also a Secret).  These are blocked
# from PUT /config.
_SECRET_ALIASES: frozenset[str] = frozenset(
    alias
    for fn in _SECRET_FIELD_NAMES
    if fn in Settings.model_fields
    and (alias := Settings.model_fields[fn].alias) is not None
)


def _is_secret_key(key: str) -> bool:
    """Return True when *key* is a secret — either a Secrets field name
    or a Settings alias that maps to one."""
    return key in _SECRET_FIELD_NAMES or key in _SECRET_ALIASES


# ---------------------------------------------------------------------------
#  Config file path resolution (for writes)
# ---------------------------------------------------------------------------


def _canonical_config_path() -> Path:
    """Return the path where config writes should land.

    When ``MILL_CONFIG_FILE`` is explicitly set, use it; otherwise
    always target ``config/config.json`` (never the example file).
    """
    explicit = os.environ.get("MILL_CONFIG_FILE")
    if explicit:
        return Path(explicit)
    return Path("config/config.json")


# ---------------------------------------------------------------------------
#  Version history
# ---------------------------------------------------------------------------


def _versions_path(data_dir: Path) -> Path:
    return data_dir / "config_versions.jsonl"


def _read_versions(data_dir: Path) -> list[dict[str, Any]]:
    """Read all version records, newest first."""
    vp = _versions_path(data_dir)
    if not vp.exists():
        return []
    versions: list[dict[str, Any]] = []
    for line in vp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            versions.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("config_versions.jsonl: skipping malformed line")
    versions.reverse()  # file is append-only (oldest first) → newest first
    return versions


def _current_version(data_dir: Path) -> int:
    versions = _read_versions(data_dir)
    return versions[0]["version"] if versions else 0


def _record_version(
    data_dir: Path,
    config_snapshot: dict[str, Any],
    changed_keys: list[str],
) -> int:
    """Append a new version record. Returns the new version number."""
    versions = _read_versions(data_dir)
    new_version = (versions[0]["version"] + 1) if versions else 1
    record: dict[str, Any] = {
        "version": new_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "changed_keys": [
            f"{k} (secret)" if _is_secret_key(k) else k for k in changed_keys
        ],
        "config": config_snapshot,
    }
    vp = _versions_path(data_dir)
    vp.parent.mkdir(parents=True, exist_ok=True)
    with open(vp, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    # Prune to last 20
    _prune_versions(data_dir, keep=20)
    return new_version


def _prune_versions(data_dir: Path, keep: int = 20) -> None:
    """Keep only the most recent *keep* versions."""
    versions = _read_versions(data_dir)
    if len(versions) <= keep:
        return
    # versions is newest-first; keep the first `keep`
    to_keep = versions[:keep]
    vp = _versions_path(data_dir)
    # Re-write in oldest-first order
    lines = [json.dumps(r, ensure_ascii=False) + "\n" for r in reversed(to_keep)]
    vp.write_text("".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
#  JSON Schema generation
# ---------------------------------------------------------------------------


def _flatten_nullable_anyof(obj: dict[str, Any]) -> None:
    """Flatten ``anyOf: [{type: null}, {type: ...}]`` → ``type: ...`` + ``nullable: true``."""
    any_of = obj.get("anyOf")
    if not isinstance(any_of, list) or len(any_of) != 2:
        return
    a, b = any_of
    if not isinstance(a, dict) or not isinstance(b, dict):
        return
    if a.get("type") == "null":
        obj["type"] = b.get("type", "string")
        obj["nullable"] = True
        if "format" in b:
            obj["format"] = b["format"]
        obj.pop("anyOf", None)
    elif b.get("type") == "null":
        obj["type"] = a.get("type", "string")
        obj["nullable"] = True
        if "format" in a:
            obj["format"] = a["format"]
        obj.pop("anyOf", None)


def _clean_obj(obj: dict[str, Any]) -> None:
    """Recursively strip pydantic-internal keys and flatten nullable anyOf."""
    for noise_key in (
        "metadata",
        "validation_alias",
        "serialization_alias",
        "json_schema_extra",
        "frozen",
        "init_var",
    ):
        obj.pop(noise_key, None)
    _flatten_nullable_anyof(obj)
    for val in obj.values():
        if isinstance(val, dict):
            _clean_obj(val)
        elif isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    _clean_obj(item)


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Simplify a pydantic-generated JSON Schema for UI consumption."""
    raw: Any = json.loads(json.dumps(schema, default=str))
    s: dict[str, Any] = raw if isinstance(raw, dict) else {}
    if "properties" in s:
        for prop in s["properties"].values():
            if isinstance(prop, dict):
                _clean_obj(prop)
    s.pop("$defs", None)
    return s


def _generate_config_schema() -> dict[str, Any]:
    """Generate the JSON Schema for the Settings model, augmented with
    secret-field entries marked as ``writeOnly``."""
    raw: dict[str, Any] = Settings.model_json_schema()
    schema: dict[str, Any] = _clean_schema(raw)

    # Add secret fields as writeOnly string entries
    if "properties" not in schema:
        schema["properties"] = {}
    for name in sorted(_SECRET_FIELD_NAMES):
        schema["properties"][name] = {
            "type": "string",
            "writeOnly": True,
            "description": "Secret — env-injected by the deploy plane. Not stored in config.json.",
        }

    return schema


# ---------------------------------------------------------------------------
#  Public API
# ---------------------------------------------------------------------------


def get_config(
    settings: Settings | None = None, data_dir: Path | None = None
) -> dict[str, Any]:
    """Return the effective config with secrets masked and the JSON Schema.

    Returns the standard ``{"config": {...}, "schema": {...}, "version": N}``
    shape.
    """
    if settings is None:
        settings = Settings()
    if data_dir is None:
        data_dir = settings.data_dir

    config: dict[str, Any] = {}

    # --- Settings fields (aliased) ---
    for field_name, field_info in Settings.model_fields.items():
        alias = field_info.alias or field_name
        value = getattr(settings, field_name, None)
        if _is_secret_key(alias) or field_name in _SECRET_FIELD_NAMES:
            # Always mask secrets per the config-ownership standard
            config[alias] = "**********"
        else:
            # Convert Path to string for JSON serialization
            if isinstance(value, Path):
                config[alias] = str(value)
            else:
                config[alias] = value

    # --- Secret fields (masked) ---
    for field_name in sorted(_SECRET_FIELD_NAMES):
        config[field_name] = "**********"

    schema = _generate_config_schema()
    version = _current_version(data_dir)

    return {"config": config, "schema": schema, "version": version}


def _validate_updates(updates: dict[str, Any]) -> None:
    """Validate each field in *updates* against the Settings model.

    Raises ``ConfigValidationError`` on the first failure.
    """
    for key, value in updates.items():
        if _is_secret_key(key):
            raise ConfigValidationError(
                f"'{key}' is a secret — secrets are env-injected by the "
                "deploy plane and cannot be updated via PUT /config"
            )
        # Resolve alias → field name
        field_name: str | None = None
        for fn, fi in Settings.model_fields.items():
            if (fi.alias or fn) == key:
                field_name = fn
                break
        if field_name is None:
            raise ConfigValidationError(f"'{key}' is not a recognized config key")

        field_info = Settings.model_fields[field_name]
        try:
            TypeAdapter(field_info.annotation).validate_python(value)
        except ValidationError as exc:
            raise ConfigValidationError(
                f"{key}: {_format_validation_error(exc)}"
            ) from exc


def _read_json_config(path: Path) -> dict[str, Any]:
    """Read the JSON config file. Returns empty dict on missing file."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigValidationError(f"Config file is not valid JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _write_json_config(path: Path, data: dict[str, Any]) -> None:
    """Write the JSON config file atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def update_config(
    updates: dict[str, Any], data_dir: Path | None = None
) -> dict[str, Any]:
    """Apply a partial config update.  Returns the new effective config.

    Rejects any key that identifies a secret.  Validates each updated
    field against the Settings model before writing.
    """
    _validate_updates(updates)

    config_path = _canonical_config_path()
    current = _read_json_config(config_path)

    # Merge into the ``settings`` block
    current_settings = current.get("settings", {})
    if not isinstance(current_settings, dict):
        current_settings = {}

    changed_keys = list(updates.keys())
    merged = {**current_settings, **updates}
    new_config = {**current, "settings": merged}

    _write_json_config(config_path, new_config)

    # Record version (store the full effective snapshot)
    if data_dir is None:
        data_dir = Settings().data_dir
    snapshot: dict[str, Any] = get_config(data_dir=data_dir)["config"]
    _record_version(data_dir, snapshot, changed_keys)

    return get_config(data_dir=data_dir)


def get_versions(data_dir: Path | None = None) -> dict[str, Any]:
    """Return recent config versions (newest first)."""
    if data_dir is None:
        data_dir = Settings().data_dir
    versions = _read_versions(data_dir)
    return {
        "versions": [
            {
                "version": v["version"],
                "timestamp": v["timestamp"],
                "changed_keys": v.get("changed_keys", []),
            }
            for v in versions
        ]
    }


def rollback_config(
    target_version: int, data_dir: Path | None = None
) -> dict[str, Any]:
    """Rollback to a previous config version.  Creates a new version."""
    if data_dir is None:
        data_dir = Settings().data_dir
    versions = _read_versions(data_dir)

    target = None
    for v in versions:
        if v["version"] == target_version:
            target = v
            break

    if target is None:
        raise ConfigValidationError(
            f"Version {target_version} not found. "
            f"Available versions: {[v['version'] for v in versions]}"
        )

    snapshot = target.get("config", {})
    if not snapshot:
        raise ConfigValidationError(
            f"Version {target_version} has no stored config snapshot"
        )

    # Validate by constructing a minimal update
    # Strip secret-masked values before writing
    clean_snapshot = {
        k: v for k, v in snapshot.items() if v != "**********" and not _is_secret_key(k)
    }

    config_path = _canonical_config_path()
    if config_path.exists():
        try:
            current = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}
    else:
        current = {}

    if not isinstance(current, dict):
        current = {}

    new_config = {**current, "settings": clean_snapshot}

    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(new_config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    tmp.replace(config_path)

    changed_keys = list(clean_snapshot.keys())
    _record_version(data_dir, get_config(data_dir=data_dir)["config"], changed_keys)

    return get_config(data_dir=data_dir)


# ---------------------------------------------------------------------------
#  Validation error
# ---------------------------------------------------------------------------


class ConfigValidationError(ValueError):
    """Raised when a config update or rollback fails validation."""


def _format_validation_error(exc: ValidationError) -> str:
    """Format a pydantic ValidationError as a single-line message."""
    errors = exc.errors()
    parts: list[str] = []
    for e in errors:
        loc = ".".join(str(p) for p in e.get("loc", []))
        msg = e.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)
