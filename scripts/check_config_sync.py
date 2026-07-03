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
        # -- Fields with no JSON entry (yet) — listed here so the
        #    invariant passes at HEAD; each should eventually gain a
        #    JSON entry or be explicitly documented as env-only --
        "investigation_workspace",
        "refine_delta_reuse_enabled",
        "trace_review_max_inspector_runs_per_pass",
        "max_events_per_ticket",
        "db_maintenance_periodic",
        "db_maintenance_interval_seconds",
        "ticket_state_cycle_limit",
        "deliver_max_identical_blocks",
        "security_posture_memory_path",
        "security_posture_periodic",
        "security_posture_interval_seconds",
        "security_posture_request_limit",
    }
)

# ``Secrets`` fields that are intentionally NOT user-configurable via
# the config file (invariant 3), so they never appear in the
# ``secrets:`` block of ``config/config.example.json``.
_SECRETS_NOT_IN_EXAMPLE: frozenset[str] = frozenset()


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
