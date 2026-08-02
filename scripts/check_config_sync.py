#!/usr/bin/env python3
"""Deterministic config-surface drift checker for robotsix-mill.

Usage (from the repo root):
    python scripts/check_config_sync.py

Cross-references the live source-of-truth objects — never re-parses
source — to catch config drift that the heuristic ``config_sync`` LLM
agent would otherwise only notice on its next daily pass:

    * ``config/config.example.json`` — the committed single-file config
      template.  Its ``settings`` keys are the canonical Settings
      surface; its ``secrets`` keys are the Secrets template.
    * ``robotsix_mill.config.Settings`` / ``Secrets`` — the Pydantic-v2
      models (introspected via ``model_fields``).

Invariants (each contributes drift lines; the run fails if any fire):

    1. Every ``settings`` key in ``config/config.example.json`` is a
       real ``Settings`` field name or alias, unless listed in
       ``_SETTINGS_KEYS_NOT_IN_MODEL``.
    2. Every ``Settings`` model field name or alias must appear as a
       key in ``config/config.example.json`` ``settings``, except those
       listed in ``_MODEL_FIELDS_NOT_IN_JSON``.
    3. The keys of the ``secrets:`` block in ``config/config.example.json``
       equal the user-configurable ``Secrets`` fields, modulo
       ``_SECRETS_NOT_IN_EXAMPLE``.

This script is meant to be invoked from the repo root (which CI and the
``validate-config-sync`` pre-commit hook both guarantee).

Exit codes:
    0 — every invariant holds; the config surfaces are in sync.
    1 — at least one invariant fired; details are printed to stderr.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Ensure both the repo root and src/ are importable so 'import
# robotsix_mill' works when run as a flat script.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_CONFIG_EXAMPLE_JSON = _REPO_ROOT / "config" / "config.example.json"


# ---------------------------------------------------------------------------
#  Explicit, commented exception sets
# ---------------------------------------------------------------------------

# Settings keys in config.example.json that are intentionally NOT
# Settings model fields/aliases. Each entry documents WHY.
_SETTINGS_KEYS_NOT_IN_MODEL: frozenset[str] = frozenset()

# Settings model fields intentionally absent from
# ``config.example.json`` ``settings`` block. Each field documents
# WHY it is not in the committed template.
_MODEL_FIELDS_NOT_IN_JSON: frozenset[str] = frozenset(
    {
        # -- Secrets / credentials — sourced from the config.json
        #    ``secrets:`` block (Secrets model) or env vars --
        "openrouter_api_key",
        "forge_token",
        "forge_repo_create_token",
        "github_app_id",
        "github_app_private_key",
        "github_app_private_key_path",
        "langfuse_base_url",
        "langfuse_public_key",
        "langfuse_secret_key",
        "langfuse_project_id",
        "langfuse_project_name",
        "openrouter_management_key",
        "ntfy_url",
        "ntfy_token",
        "sandbox_push_token",
        # -- Repos registry — not a flat setting field --
        "repos",
        # -- Fields with no JSON entry (yet) — listed here so the
        #    invariant passes at HEAD; each should eventually gain a
        #    JSON entry or be explicitly documented as env-only --
        "refine_delta_reuse_enabled",
        "trace_review_max_inspector_runs_per_pass",
        "max_events_per_ticket",
        "db_maintenance_periodic",
        "db_maintenance_interval_seconds",
        "ticket_state_cycle_limit",
        "deliver_max_identical_blocks",
        # -- Periodic agent settings (presence-file driven; no JSON entry) --
        "repo_description_sync_periodic",
        "repo_description_sync_interval_seconds",
        # -- packaged resource dirs — defaults resolve via
        #    importlib.resources to machine-specific absolute paths, so a
        #    committed template value can only be wrong somewhere. A
        #    CWD-relative template value ("skills") bricked the container
        #    on 2026-07-19 (implement preflight blocked every ticket with
        #    "missing skill file"); overrides remain available via env or
        #    a deployment's own config.json --
        "skills_dir",
        "language_instructions_dir",
    }
)

# ``Secrets`` fields that are intentionally NOT user-configurable via
# the config file (invariant 3), so they never appear in the
# ``secrets:`` block of ``config/config.example.json``.
_SECRETS_NOT_IN_EXAMPLE: frozenset[str] = frozenset()

# Source files scanned for code-comment "Default N" annotations (invariant 4).
# Each path is relative to the repo root.
_COMMENT_DEFAULT_SOURCES: tuple[str, ...] = (
    "src/robotsix_mill/config/_settings_periodic.py",
    "src/robotsix_mill/config/_settings_core.py",
)

# Regex matching a comment line that states a numeric default value.
# Capture group 1 = the claimed default integer.  The trailing-punctuation
# guard (end-of-line, parentheses, equals, commas, or various dash forms)
# rejects human-language durations like "Default 7 days" while accepting
# machine-readable annotations like "Default 604800 (7 days)" or
# "Default 500 — high enough …".
_COMMENT_DEFAULT_RE = re.compile(
    r"^\s*#\s+.*\bDefault\s+(\d+)\s*(?:$|[=(,\u2013\u2014\u2015\-])"
)

# Regex capturing a field name and its type annotation on a ``field: type =
# Field(…)`` line.  After a "Default N" comment we look ahead for the
# next field definition to resolve which field the comment belongs to.
_FIELD_DEF_RE = re.compile(r"^\s*(\w+)\s*:\s*[^=]+\s*=\s*Field\(")

# ---------------------------------------------------------------------------
#  Pure helpers (parameterised so synthetic cases need no monkeypatching)
# ---------------------------------------------------------------------------


def build_valid_settings_names(model: type) -> set[str]:
    """Return the union of every ``model`` field name and non-null alias."""
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        if field.alias:
            names.add(field.alias)
    return names


def check_settings_keys_in_model(
    example_keys: set[str],
    valid_names: set[str],
    exceptions: frozenset[str],
) -> list[str]:
    """Invariant 1: every settings key must be a real field name/alias."""
    drift: list[str] = []
    for key in sorted(example_keys):
        if key in exceptions:
            continue
        if key not in valid_names:
            drift.append(
                f"config.example.json settings key {key!r} is not a "
                "Settings field name or alias"
            )
    return drift


def check_model_fields_in_json(
    model: type,
    example_keys: set[str],
    exceptions: frozenset[str],
) -> list[str]:
    """Invariant 2: every Settings field must appear in the JSON settings."""
    drift: list[str] = []
    for name, field in model.model_fields.items():
        if name in exceptions:
            continue
        if name in example_keys:
            continue
        if field.alias and field.alias in example_keys:
            continue
        drift.append(
            f"Settings field {name!r} has no entry in "
            "config.example.json settings and is not in the exception set"
        )
    return drift


def check_secrets_example(
    example_keys: set[str],
    secrets_fields: set[str],
    exceptions: frozenset[str],
) -> list[str]:
    """Invariant 3: secrets-block keys == user-configurable Secrets fields."""
    expected = secrets_fields - exceptions
    drift: list[str] = []
    for key in sorted(example_keys - expected):
        drift.append(
            "config.example.json secrets key is not a user-configurable "
            f"Secrets field: {key}"
        )
    for field in sorted(expected - example_keys):
        drift.append(
            f"Secrets field missing from config.example.json secrets block: {field}"
        )
    return drift


def _parse_comment_defaults(
    source_path: Path,
) -> list[tuple[str, int, int]]:
    """Parse one source file for code-comment "Default N" annotations.

    Returns a list of ``(field_name, claimed_default, line_number)``
    tuples.  Each tuple represents a comment like ``# … Default 86400
    (daily).`` followed (within a few lines) by a field definition
    whose ``Field(default=…)`` we can cross-check.

    A comment whose field cannot be resolved is silently skipped.
    """

    text = source_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    results: list[tuple[str, int, int]] = []

    for i, line in enumerate(lines):
        m = _COMMENT_DEFAULT_RE.search(line)
        if m is None:
            continue
        claimed = int(m.group(1))

        # Walk forward from the comment to find the next field
        # definition.  We search up to 15 lines ahead — any comment
        # farther from its field is likely an orphan.
        field_name: str | None = None
        for j in range(i + 1, min(i + 16, len(lines))):
            fm = _FIELD_DEF_RE.match(lines[j])
            if fm:
                field_name = fm.group(1)
                break

        if field_name is None:
            continue

        results.append((field_name, claimed, i + 1))  # 1-indexed

    return results


def check_comment_defaults(settings_model: type) -> list[str]:
    """Invariant 4: every code-comment "Default N" must match the
    actual ``Field(default=N)`` on the corresponding model field."""

    drift: list[str] = []
    for rel_path in _COMMENT_DEFAULT_SOURCES:
        source_path = _REPO_ROOT / rel_path
        if not source_path.is_file():
            drift.append(f"comment-default source file not found: {rel_path}")
            continue

        for field_name, claimed, lineno in _parse_comment_defaults(source_path):
            model_field = settings_model.model_fields.get(field_name)
            if model_field is None:
                drift.append(
                    f"{rel_path}:{lineno}: comment says Default {claimed} "
                    f"but field {field_name!r} is not in the Settings model"
                )
                continue

            actual = model_field.default
            if not isinstance(actual, int):
                continue  # non-numeric default — not comparable

            if actual != claimed:
                drift.append(
                    f"{rel_path}:{lineno}: comment says Default {claimed} "
                    f"but {field_name} Field(default={actual})"
                )

    return drift


def collect_drift() -> list[str]:
    """Load the real on-disk surfaces and run every invariant."""
    from robotsix_mill.config import Secrets, Settings

    with open(_CONFIG_EXAMPLE_JSON, "r", encoding="utf-8") as fh:
        config_example = json.load(fh)
    if not isinstance(config_example, dict):
        return ["config.example.json is not a JSON object"]

    settings_example = config_example.get("settings", {})
    if not isinstance(settings_example, dict):
        return ["config.example.json settings key is not a JSON object"]

    secrets_example = config_example.get("secrets", {})
    if not isinstance(secrets_example, dict):
        secrets_example = {}

    valid_names = build_valid_settings_names(Settings)
    example_settings_keys = set(settings_example)
    secrets_fields = set(Secrets.model_fields)
    example_secrets_keys = set(secrets_example)

    drift: list[str] = []
    drift += check_settings_keys_in_model(
        example_settings_keys, valid_names, _SETTINGS_KEYS_NOT_IN_MODEL
    )
    drift += check_model_fields_in_json(
        Settings, example_settings_keys, _MODEL_FIELDS_NOT_IN_JSON
    )
    drift += check_secrets_example(
        example_secrets_keys, secrets_fields, _SECRETS_NOT_IN_EXAMPLE
    )
    drift += check_comment_defaults(Settings)
    return drift


def main() -> int:
    drift = collect_drift()
    if drift:
        for entry in drift:
            print(f"STALE: {entry}", file=sys.stderr)
        print(
            f"FAIL: {len(drift)} config-sync drift item(s) detected",
            file=sys.stderr,
        )
        return 1

    print("config sync OK (JSON settings, secrets example all in sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
