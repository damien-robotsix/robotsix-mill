#!/usr/bin/env python3
"""One-shot migration tool: .env + secrets.env → config/*.yaml.

Converts the legacy dotenv configuration files into the YAML-only
config system (RFC config-v2).  Non-destructive — the original files
are left untouched.

Usage::

    python dev/migrate-env-to-yaml.py          # from repo root
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is on sys.path so we can import the config loader.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import yaml
from robotsix_mill.config_loader import _YAML_PATH_TO_ALIAS, load_yaml_config


# ---------------------------------------------------------------------------
#  Dotenv parser
# ---------------------------------------------------------------------------


def parse_dotenv(path: str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` dotenv file into a flat dict.

    Skips blank lines and comment lines (``#`` as first non-whitespace
    character).  Strips inline comments — a ``#`` preceded by one or
    more whitespace characters.  Splits on the first ``=`` only so
    values may contain ``=``.
    """
    result: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            # Strip inline comment: # preceded by whitespace
            stripped = re.sub(r"\s+#.*$", "", stripped)
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            result[key] = value
    return result


# ---------------------------------------------------------------------------
#  Nested-dict helpers
# ---------------------------------------------------------------------------


def _get_nested(d: dict[str, Any], path: str) -> object:
    """Retrieve a value from a nested dict by dotted path.

    Raises ``KeyError`` if any segment is missing or if an intermediate
    segment is not a dict.
    """
    parts = path.split(".")
    current: object = d
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def _nest_dict(flat: dict[str, object]) -> dict[str, Any]:
    """Convert a ``{dotted.path: value}`` mapping into a nested dict."""
    result: dict[str, Any] = {}
    for path, value in sorted(flat.items()):
        parts = path.split(".")
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return result


# ---------------------------------------------------------------------------
#  Type coercion
# ---------------------------------------------------------------------------


def _coerce(env_value: str, default_value: object) -> object:
    """Coerce an env-var string to match the Python type of *default_value*.

    * ``None``     — empty string → ``None``; non-empty → kept as string
    * ``bool``     — ``true`` / ``1`` / ``yes`` (case-insensitive) → ``True``
    * ``int``      — ``int(env_value)``
    * ``float``    — ``float(env_value)``
    * ``str``      — kept verbatim
    """
    if default_value is None:
        return None if env_value == "" else env_value
    if isinstance(default_value, bool):
        return env_value.lower() in ("true", "1", "yes")
    if isinstance(default_value, int):
        return int(env_value)
    if isinstance(default_value, float):
        return float(env_value)
    return env_value


# ---------------------------------------------------------------------------
#  Idempotent file writer
# ---------------------------------------------------------------------------


def _write_if_changed(path: str, content: str, label: str) -> bool:
    """Write *content* to *path*, but only when different from the
    existing file on disk.

    Returns ``True`` when the file was created or updated, ``False``
    when it is already byte-identical.
    """
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            existing = fh.read()
        if existing == content:
            print(f"{label} is up to date")
            return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"Created {label}")
    return True


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)

    # -- .env file --------------------------------------------------------
    dotenv_path = repo_root / ".env"
    if not dotenv_path.exists():
        print(
            "Error: .env not found in repo root — nothing to migrate.",
            file=sys.stderr,
        )
        sys.exit(1)

    # -- config/mill.defaults.yaml ----------------------------------------
    defaults_path = repo_root / "config" / "mill.defaults.yaml"
    if not defaults_path.exists():
        print(
            f"Error: {defaults_path} not found — cannot diff against defaults.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Parse .env
    env_vars = parse_dotenv(str(dotenv_path))

    # Build reverse alias map: alias → dotted YAML path.
    # For aliases that appear under multiple YAML paths, the last
    # insertion-order entry wins — this is deterministic and matches
    # flatten_yaml_config semantics.
    reverse_alias: dict[str, str] = {}
    for yaml_path, alias in _YAML_PATH_TO_ALIAS.items():
        reverse_alias[alias] = yaml_path

    # Load defaults (Layer 1 only — no local overrides)
    defaults = load_yaml_config(skip_local=True)

    # Diff: for each env var with a known alias, compare to default
    overrides: dict[str, object] = {}
    unmapped: list[str] = []

    for var_name, var_value in sorted(env_vars.items()):
        if var_name not in reverse_alias:
            unmapped.append(var_name)
            continue

        yaml_path = reverse_alias[var_name]
        try:
            default_val = _get_nested(defaults, yaml_path)
        except KeyError:
            # Path exists in alias map but not in defaults — unusual;
            # treat as unmapped for safety.
            unmapped.append(var_name)
            continue

        try:
            coerced = _coerce(var_value, default_val)
        except (ValueError, TypeError):
            print(
                f"Warning: could not coerce {var_name}={var_value!r} "
                f"to {type(default_val).__name__} — skipping.",
                file=sys.stderr,
            )
            continue

        if coerced != default_val:
            overrides[yaml_path] = coerced

    # -- Write config/mill.local.yaml ------------------------------------
    local_yaml_path = repo_root / "config" / "mill.local.yaml"

    if overrides:
        nested = _nest_dict(overrides)
        yaml_body = yaml.dump(
            nested, sort_keys=True, default_flow_style=False, allow_unicode=True
        )
        header = (
            "# Generated by dev/migrate-env-to-yaml.py — local overrides relative to\n"
            "# config/mill.defaults.yaml.  Edit freely; re-running the migration\n"
            "# regenerates this file from .env (non-destructive).\n"
            "\n"
        )
        _write_if_changed(
            str(local_yaml_path),
            header + yaml_body,
            f"config/mill.local.yaml with {len(overrides)} override(s)",
        )
    elif local_yaml_path.exists():
        print("config/mill.local.yaml is up to date (no overrides)")
    else:
        print("config/mill.local.yaml is up to date (no overrides; no file created)")

    # -- secrets.env → config/secrets.yaml -------------------------------
    secrets_path = repo_root / "secrets.env"
    secrets_yaml_path = repo_root / "config" / "secrets.yaml"

    if not secrets_path.exists():
        print("secrets.env not found — skipping secrets migration.")
    else:
        secrets_vars = parse_dotenv(str(secrets_path))
        # Lowercase all keys to match Secrets model field names
        secrets_data: dict[str, str] = {}
        for k, v in secrets_vars.items():
            secrets_data[k.lower()] = v

        secrets_yaml_body = yaml.dump(
            secrets_data, sort_keys=True, default_flow_style=False, allow_unicode=True
        )
        header = (
            "# Generated by dev/migrate-env-to-yaml.py from secrets.env.\n"
            "# Permissions: 0600 (owner read/write only).\n"
            "\n"
        )
        _write_if_changed(
            str(secrets_yaml_path),
            header + secrets_yaml_body,
            f"config/secrets.yaml with {len(secrets_data)} secret(s)",
        )
        # Always (re)set restrictive permissions
        os.chmod(str(secrets_yaml_path), 0o600)

    # -- Unmapped variable warning ---------------------------------------
    if unmapped:
        print(
            f"Warning: {len(unmapped)} unmapped .env variable(s) skipped: "
            f"{', '.join(unmapped)}",
            file=sys.stderr,
        )
        print(
            "These variables have no YAML mapping and must be handled manually.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
