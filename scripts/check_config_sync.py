#!/usr/bin/env python3
"""Check that config artifacts are in sync with ``MailConfig``.

Cross-references the canonical ``MailConfig`` field list (obtained via
``dataclasses.fields()``) against three user-facing artifacts:

1. ``config/mail.local.example.yaml``
2. ``.env.example``
3. ``docs/connecting.md``

Exits 0 when in sync, 1 when drift is found, 2 on a script-level error.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Make src/ importable both when run directly and when imported by tests.
# ---------------------------------------------------------------------------
_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from robotsix_auto_mail.config import MailConfig  # noqa: E402

# ---------------------------------------------------------------------------
# Hard-coded field mappings (second pair of eyes on the mapping logic).
# ---------------------------------------------------------------------------

FIELD_TO_YAML: dict[str, str] = {
    "imap_host": "imap.host",
    "imap_port": "imap.port",
    "imap_tls_mode": "imap.tls_mode",
    "imap_folder": "imap.folder",
    "smtp_host": "smtp.host",
    "smtp_port": "smtp.port",
    "smtp_tls_mode": "smtp.tls_mode",
    "username": "auth.username",
    "password": "auth.password",
    "db_path": "store.path",
    "llm_api_key": "llm.api_key",
    "llm_model": "llm.model",
    "ingest_interval_minutes": "ingest.interval_minutes",
}

FIELD_TO_ENV: dict[str, str] = {
    "imap_host": "MAIL_IMAP_HOST",
    "imap_port": "MAIL_IMAP_PORT",
    "imap_tls_mode": "MAIL_IMAP_TLS_MODE",
    "imap_folder": "MAIL_IMAP_FOLDER",
    "smtp_host": "MAIL_SMTP_HOST",
    "smtp_port": "MAIL_SMTP_PORT",
    "smtp_tls_mode": "MAIL_SMTP_TLS_MODE",
    "username": "MAIL_USERNAME",
    "password": "MAIL_PASSWORD",
    "db_path": "MAIL_DB_PATH",
    "llm_api_key": "LLM_API_KEY",
    "llm_model": "LLM_MODEL",
    "ingest_interval_minutes": "MAIL_INGEST_INTERVAL",
}

# ---------------------------------------------------------------------------
# Placeholder patterns — values that are NOT default-mismatches.
# ---------------------------------------------------------------------------

_PLACEHOLDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^sk-or-v1-…$"),
    re.compile(r"^sk-or-v1-\w+$"),
    re.compile(r"^your-password-here$"),
]

# ---------------------------------------------------------------------------
# Meta env vars / YAML keys excluded from "stale" checks.
# ---------------------------------------------------------------------------

_ENV_EXCLUDE_STALE: frozenset[str] = frozenset({"MAIL_CONFIG_PATH"})


# ====================================================================
# Self-consistency check
# ====================================================================


def _self_consistency_check() -> None:
    """Verify mapping dicts are 1:1 with ``MailConfig`` fields."""
    fields = {f.name for f in dataclasses.fields(MailConfig)}

    for field_name in fields:
        if field_name not in FIELD_TO_YAML:
            _fail(
                f"Internal error: field {field_name!r} missing from "
                f"FIELD_TO_YAML mapping"
            )
        if field_name not in FIELD_TO_ENV:
            _fail(
                f"Internal error: field {field_name!r} missing from "
                f"FIELD_TO_ENV mapping"
            )

    for field_name in FIELD_TO_YAML:
        if field_name not in fields:
            _fail(
                f"Internal error: FIELD_TO_YAML key {field_name!r} "
                f"is not a MailConfig field"
            )
    for field_name in FIELD_TO_ENV:
        if field_name not in fields:
            _fail(
                f"Internal error: FIELD_TO_ENV key {field_name!r} "
                f"is not a MailConfig field"
            )


# ====================================================================
# Helpers
# ====================================================================


def _fail(message: str) -> None:
    """Print *message* to stderr and exit 2 (script error)."""
    print(message, file=sys.stderr)
    sys.exit(2)


def _is_placeholder(value: str) -> bool:
    """Return True when *value* is a known placeholder string."""
    for pattern in _PLACEHOLDER_PATTERNS:
        if pattern.match(value):
            return True
    return False


_MISSING_SENTINEL = object()


def _get_nested(data: dict[str, Any], path: str) -> Any:
    """Return the value at *path* (dotted, e.g. ``imap.host``) in *data*.

    Returns ``_MISSING_SENTINEL`` when any segment is missing.
    """
    keys = path.split(".")
    for key in keys:
        if not isinstance(data, dict):
            return _MISSING_SENTINEL
        val = data.get(key, _MISSING_SENTINEL)
        if val is _MISSING_SENTINEL:
            return _MISSING_SENTINEL
        data = val  # type: ignore[assignment]
    return data


def _field_default(field: dataclasses.Field[Any]) -> Any:
    """Return *field*'s default, or ``dataclasses.MISSING``."""
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:
        return field.default_factory()
    return dataclasses.MISSING


def _values_match(
    artifact_value: Any,
    mailconfig_default: Any,
    *,
    raw_string: str = "",
) -> bool:
    """Return True when *artifact_value* matches the MailConfig default.

    Handles type coercion for commented-out YAML values (which arrive
    as strings) and placeholder tolerance.
    """
    # If the raw string is a known placeholder, skip comparison.
    if raw_string and _is_placeholder(raw_string):
        return True

    # If the value itself looks like a placeholder string, also skip.
    if isinstance(artifact_value, str) and _is_placeholder(artifact_value):
        return True

    # Required fields (MISSING default) — no comparison performed.
    if mailconfig_default is dataclasses.MISSING:
        return True

    # Direct match.
    if artifact_value == mailconfig_default:
        return True

    # Artifact value might be a string but the default is a different
    # type (e.g. commented-out port "993" vs default int 993).
    # Try YAML-parsing the string form.
    if isinstance(artifact_value, str):
        try:
            parsed = yaml.safe_load(artifact_value)
        except yaml.YAMLError:
            return False
        if parsed == mailconfig_default:
            return True
        # Handle "993" → "993" (str) vs 993 (int) — already caught
        # by safe_load giving int 993.  But also handle quoted strings
        # like '"direct-tls"' → direct-tls.
        if isinstance(parsed, str) and isinstance(mailconfig_default, str):
            if parsed == mailconfig_default:
                return True

    return False


# ====================================================================
# Check 1 — YAML example file
# ====================================================================


def _scan_commented_yaml(text: str) -> dict[str, str]:
    """Extract commented-out key=value pairs from YAML *text*.

    Returns a ``{dotted.path: raw_value_string}`` dict.
    """
    result: dict[str, str] = {}
    current_section: str | None = None
    in_commented_section = False

    for line in text.splitlines():
        # Active section header: ``section_name:`` at indent 0.
        m = re.match(r"^(\w+):\s*(?:#.*)?$", line)
        if m:
            current_section = m.group(1)
            in_commented_section = False
            continue

        # Commented section header: ``# section_name:`` at indent 0.
        m = re.match(r"^# (\w+):\s*$", line)
        if m:
            current_section = m.group(1)
            in_commented_section = True
            continue

        if current_section is None:
            continue

        if in_commented_section:
            # ``#   key: value``
            m = re.match(r"^#   (\w+):\s*(.*)$", line)
        else:
            # ``  # key: value``
            m = re.match(r"^  # (\w+):\s*(.*)$", line)

        if m:
            key = m.group(1)
            value = m.group(2).strip()
            result[f"{current_section}.{key}"] = value

    return result


def check_yaml_example(
    text: str,
    path: str = "config/mail.local.example.yaml",
) -> list[dict[str, Any]]:
    """Check *text* (the YAML example file) against ``MailConfig``.

    Returns a list of finding dicts.
    """
    findings: list[dict[str, Any]] = []

    # -- structured parse (uncommented keys) --------------------------------
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError:
        return [{"artifact": path, "type": "yaml-parse-error"}]

    if data is None:
        data = {}
    if not isinstance(data, dict):
        return [{"artifact": path, "type": "yaml-parse-error"}]

    # -- text scan (commented-out keys) -------------------------------------
    commented = _scan_commented_yaml(text)

    # -- collect all YAML keys (both sources) -------------------------------
    all_yaml_keys: set[str] = set()

    # Add structured keys (nested paths).
    def _collect_paths(d: dict[str, Any], prefix: str) -> None:
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else k
            all_yaml_keys.add(full)
            if isinstance(v, dict):
                _collect_paths(v, full)

    _collect_paths(data, "")

    # Add commented-out keys.
    all_yaml_keys.update(commented.keys())

    # Build reverse mapping: YAML path → field name.
    yaml_to_field: dict[str, str] = {}
    for field_name, ypath in FIELD_TO_YAML.items():
        yaml_to_field[ypath] = field_name

    # -- check each MailConfig field ----------------------------------------
    field_defaults: dict[str, Any] = {}
    for f in dataclasses.fields(MailConfig):
        field_defaults[f.name] = _field_default(f)

    for field_name, ypath in FIELD_TO_YAML.items():
        has_structured = _get_nested(data, ypath) is not _MISSING_SENTINEL
        has_commented = ypath in commented

        if not has_structured and not has_commented:
            findings.append(
                {
                    "artifact": path,
                    "type": "missing-from-yaml",
                    "key": ypath,
                    "field": field_name,
                }
            )
            continue

        default = field_defaults[field_name]
        if default is dataclasses.MISSING:
            continue  # required field — presence check was enough

        if has_structured:
            actual = _get_nested(data, ypath)
            if not _values_match(actual, default):
                findings.append(
                    {
                        "artifact": path,
                        "type": "default-mismatch",
                        "key": ypath,
                        "expected": default,
                        "actual": actual,
                    }
                )
        elif has_commented:
            raw = commented[ypath]
            if not _values_match(raw, default, raw_string=raw):
                findings.append(
                    {
                        "artifact": path,
                        "type": "default-mismatch",
                        "key": ypath,
                        "expected": default,
                        "actual": raw,
                    }
                )

    # -- stale YAML keys ----------------------------------------------------
    for ypath in all_yaml_keys:
        if ypath not in yaml_to_field:
            # Only flag leaf keys that look like config values (skip
            # parent dicts like "imap", "smtp", etc.)
            if "." not in ypath:
                # Top-level keys like "imap" are sections, not fields.
                # We don't flag them as stale.
                continue
            findings.append(
                {
                    "artifact": path,
                    "type": "stale-yaml-key",
                    "key": ypath,
                }
            )

    return findings


# ====================================================================
# Check 2 — .env.example
# ====================================================================


def _parse_env_example(text: str) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` env-example file into a dict."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip()
    return result


def check_env_example(
    text: str,
    path: str = ".env.example",
) -> list[dict[str, Any]]:
    """Check *text* (the ``.env.example`` file) against ``MailConfig``.

    Returns a list of finding dicts.
    """
    findings: list[dict[str, Any]] = []
    env_vars = _parse_env_example(text)

    field_defaults: dict[str, Any] = {}
    for f in dataclasses.fields(MailConfig):
        field_defaults[f.name] = _field_default(f)

    # Build reverse mapping: env var → field name.
    env_to_field: dict[str, str] = {}
    for field_name, ekey in FIELD_TO_ENV.items():
        env_to_field[ekey] = field_name

    # -- check each MailConfig field ----------------------------------------
    for field_name, ekey in FIELD_TO_ENV.items():
        if ekey not in env_vars:
            findings.append(
                {
                    "artifact": path,
                    "type": "missing-from-env-example",
                    "key": ekey,
                    "field": field_name,
                }
            )
            continue

        default = field_defaults[field_name]
        if default is dataclasses.MISSING:
            continue  # required field — presence check was enough

        actual = env_vars[ekey]
        if not _values_match(actual, default, raw_string=actual):
            findings.append(
                {
                    "artifact": path,
                    "type": "default-mismatch",
                    "key": ekey,
                    "expected": default,
                    "actual": actual,
                }
            )

    # -- stale env vars -----------------------------------------------------
    for ekey in env_vars:
        if ekey in _ENV_EXCLUDE_STALE:
            continue
        if ekey not in env_to_field:
            findings.append(
                {
                    "artifact": path,
                    "type": "stale-env-example-var",
                    "key": ekey,
                }
            )

    return findings


# ====================================================================
# Check 3 — docs/connecting.md
# ====================================================================


def _parse_md_table(text: str, section_heading: str) -> list[dict[str, str]]:
    """Parse the first pipe table after *section_heading* in *text*.

    Returns a list of dicts with keys from the header row.
    """
    # Find the section heading.
    heading_idx = text.find(section_heading)
    if heading_idx == -1:
        return []

    # Find the first table after the heading.  A table starts with a
    # line matching ``| ... |`` followed by a separator line
    # ``|---|...|``.
    rest = text[heading_idx:]
    lines = rest.splitlines()

    table_start: int | None = None
    for i, line in enumerate(lines):
        if line.strip().startswith("|") and "---" not in line:
            # Potential first row.  Check if the next line is a separator.
            if i + 1 < len(lines) and re.match(
                r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]
            ):
                table_start = i
                break

    if table_start is None:
        return []

    # Parse header.
    header_line = lines[table_start]
    headers = [h.strip() for h in header_line.split("|")[1:-1]]

    # Skip header and separator, parse data rows.
    rows: list[dict[str, str]] = []
    for line in lines[table_start + 2 :]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = [c.strip() for c in stripped.split("|")[1:-1]]
        if len(cells) != len(headers):
            continue
        row = dict(zip(headers, cells))
        rows.append(row)

    return rows


def _strip_backticks(s: str) -> str:
    """Remove surrounding backtick quotes from *s*."""
    if s.startswith("`") and s.endswith("`"):
        return s[1:-1]
    return s


def _normalise_doc_default(raw: str) -> Any:
    """Convert a documented default value to a Python object.

    Returns ``dataclasses.MISSING`` when the doc says "–" (none).
    """
    stripped = _strip_backticks(raw.strip())
    if stripped in ("–", "—", "-", "N/A", ""):
        return dataclasses.MISSING
    # YAML-parse: bare numbers, quoted strings, etc.
    try:
        return yaml.safe_load(stripped)
    except yaml.YAMLError:
        return stripped


def check_docs_connecting(
    text: str,
    path: str = "docs/connecting.md",
) -> list[dict[str, Any]]:
    """Check *text* (``docs/connecting.md``) against ``MailConfig``.

    Returns a list of finding dicts.
    """
    findings: list[dict[str, Any]] = []

    # -- parse the two tables -----------------------------------------------
    yaml_rows = _parse_md_table(text, "### YAML config file")
    env_rows = _parse_md_table(text, "### Environment variables")

    if not yaml_rows:
        findings.append(
            {
                "artifact": path,
                "type": "doc-parse-error",
                "message": "Could not parse YAML keys table",
            }
        )
    if not env_rows:
        findings.append(
            {
                "artifact": path,
                "type": "doc-parse-error",
                "message": "Could not parse Environment variables table",
            }
        )

    field_defaults: dict[str, Any] = {}
    for f in dataclasses.fields(MailConfig):
        field_defaults[f.name] = _field_default(f)

    # -- YAML keys table ----------------------------------------------------

    # Map YAML path → row data.
    yaml_table: dict[str, dict[str, str]] = {}
    for row in yaml_rows:
        key_cell = row.get("Key", "")
        ypath = _strip_backticks(key_cell)
        if ypath:
            yaml_table[ypath] = row

    for field_name, ypath in FIELD_TO_YAML.items():
        if ypath not in yaml_table:
            findings.append(
                {
                    "artifact": path,
                    "type": "doc-missing-yaml-key",
                    "key": ypath,
                    "field": field_name,
                }
            )
            continue

        default = field_defaults[field_name]
        if default is dataclasses.MISSING:
            continue

        row = yaml_table[ypath]
        doc_default_raw = row.get("Default", "")
        doc_default = _normalise_doc_default(doc_default_raw)

        if doc_default is dataclasses.MISSING:
            # Doc says "–" → no default documented.  Treat an
            # empty-string MailConfig default as equivalent ("no value").
            if default == "":
                continue
            findings.append(
                {
                    "artifact": path,
                    "type": "doc-default-mismatch",
                    "key": ypath,
                    "expected": default,
                    "actual": "(none documented)",
                }
            )
            continue

        if doc_default != default:
            findings.append(
                {
                    "artifact": path,
                    "type": "doc-default-mismatch",
                    "key": ypath,
                    "expected": default,
                    "actual": doc_default_raw,
                }
            )

    # Stale YAML rows.
    for ypath in yaml_table:
        if ypath not in FIELD_TO_YAML.values():
            findings.append(
                {
                    "artifact": path,
                    "type": "doc-stale-yaml-key",
                    "key": ypath,
                }
            )

    # -- Environment variables table ----------------------------------------

    env_table: dict[str, dict[str, str]] = {}
    for row in env_rows:
        var_cell = row.get("Variable", "")
        var_name = _strip_backticks(var_cell)
        if var_name:
            env_table[var_name] = row

    for field_name, ekey in FIELD_TO_ENV.items():
        if ekey not in env_table:
            findings.append(
                {
                    "artifact": path,
                    "type": "doc-missing-env-var",
                    "key": ekey,
                    "field": field_name,
                }
            )
            continue

        default = field_defaults[field_name]
        if default is dataclasses.MISSING:
            continue

        row = env_table[ekey]
        doc_default_raw = row.get("Default", "")
        doc_default = _normalise_doc_default(doc_default_raw)

        if doc_default is dataclasses.MISSING:
            # Doc says "–" → no default documented.  Treat an
            # empty-string MailConfig default as equivalent ("no value").
            if default == "":
                continue
            findings.append(
                {
                    "artifact": path,
                    "type": "doc-default-mismatch",
                    "key": ekey,
                    "expected": default,
                    "actual": "(none documented)",
                }
            )
            continue

        if doc_default != default:
            findings.append(
                {
                    "artifact": path,
                    "type": "doc-default-mismatch",
                    "key": ekey,
                    "expected": default,
                    "actual": doc_default_raw,
                }
            )

    # Stale env var rows.
    for var_name in env_table:
        if var_name in _ENV_EXCLUDE_STALE:
            continue
        if var_name not in FIELD_TO_ENV.values():
            findings.append(
                {
                    "artifact": path,
                    "type": "doc-stale-env-var",
                    "key": var_name,
                }
            )

    return findings


# ====================================================================
# Main entry point
# ====================================================================


def _repo_root() -> Path:
    """Return the repo root (parent of the ``scripts/`` directory)."""
    return Path(__file__).resolve().parent.parent


def run_checks(
    repo_root: Path | None = None,
) -> int:
    """Run all three checks.  Returns exit code 0, 1, or 2.

    Args:
        repo_root: Path to the repository root.  Defaults to auto-detection.
    """
    if repo_root is None:
        repo_root = _repo_root()

    # -- self-consistency first ---------------------------------------------
    try:
        _self_consistency_check()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    # -- load artifact files ------------------------------------------------
    yaml_path = repo_root / "config" / "mail.local.example.yaml"
    env_path = repo_root / ".env.example"
    docs_path = repo_root / "docs" / "connecting.md"

    try:
        yaml_text = yaml_path.read_text()
    except FileNotFoundError:
        print(
            f"ERROR: {yaml_path} not found — cannot run YAML check",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"ERROR: cannot read {yaml_path}: {exc}", file=sys.stderr)
        return 2

    try:
        env_text = env_path.read_text()
    except FileNotFoundError:
        print(
            f"ERROR: {env_path} not found — cannot run env-example check",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"ERROR: cannot read {env_path}: {exc}", file=sys.stderr)
        return 2

    try:
        docs_text = docs_path.read_text()
    except FileNotFoundError:
        print(
            f"ERROR: {docs_path} not found — cannot run docs check",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"ERROR: cannot read {docs_path}: {exc}", file=sys.stderr)
        return 2

    # -- run checks ---------------------------------------------------------
    findings: list[dict[str, Any]] = []
    findings.extend(check_yaml_example(yaml_text, str(yaml_path)))
    findings.extend(check_env_example(env_text, str(env_path)))
    findings.extend(check_docs_connecting(docs_text, str(docs_path)))

    # -- report -------------------------------------------------------------
    if not findings:
        print("OK")
        return 0

    for f in findings:
        ftype = f.get("type", "unknown")
        artifact = f.get("artifact", "?")
        key = f.get("key", "?")
        expected = f.get("expected", None)
        actual = f.get("actual", None)
        if expected is not None and actual is not None:
            print(
                f"{artifact}: {ftype}: {key} — "
                f"expected {expected!r}, got {actual!r}",
                file=sys.stderr,
            )
        else:
            extra = f.get("field", "") or f.get("message", "")
            if extra:
                print(
                    f"{artifact}: {ftype}: {key} ({extra})",
                    file=sys.stderr,
                )
            else:
                print(
                    f"{artifact}: {ftype}: {key}",
                    file=sys.stderr,
                )

    return 1


def main() -> None:
    """Entry point for the console script."""
    sys.exit(run_checks())


if __name__ == "__main__":
    main()
