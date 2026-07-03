#!/usr/bin/env python3
"""Deterministic config-surface drift checker for robotsix-mill.

Usage (from the repo root):
    python scripts/check_config_sync.py

Cross-references the live source-of-truth objects — never re-parses
source — to catch config drift that the heuristic ``config_sync`` LLM
agent would otherwise only notice on its next daily pass:

    * ``robotsix_mill.config.Settings`` / ``Secrets`` — the Pydantic-v2
      models (introspected via ``model_fields``).
    * ``config/config.example.json`` — the committed single-file config
      template.  Its ``settings:`` block is the canonical defaults
      surface; its ``secrets:`` block is the secrets template.

Invariants (each contributes drift lines; the run fails if any fire):

    1. Every key in ``config.example.json`` → ``settings`` is a real
       ``Settings`` field name or alias.
    2. Every ``Settings`` model field name or alias must appear as a
       key in the JSON ``settings`` block, except those listed in
       ``_MODEL_FIELDS_NOT_IN_CONFIG``.
    3. The keys of the ``secrets:`` block in ``config/config.example.json``
       equal the user-configurable ``Secrets`` fields.

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

# Ensure src/ is importable so 'import robotsix_mill' works when run
# as a flat script.
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

# Settings model fields intentionally absent from the JSON ``settings:``
# block (invariant 2). Each entry documents WHY the field is not in the
# JSON config.
_MODEL_FIELDS_NOT_IN_CONFIG: frozenset[str] = frozenset(
    {
        # -- Secrets / credentials — sourced from the config.json
        #    ``secrets:`` block (Secrets model) or env vars --
        "forge_token",
        "github_app_id",
        "github_app_private_key",
        "langfuse_base_url",
        "langfuse_project_id",
        "langfuse_public_key",
        "langfuse_secret_key",
        "ntfy_token",
        "ntfy_url",
        "openrouter_api_key",
        # Also their aliases (FORGE_TOKEN, etc.)
        "FORGE_TOKEN",
        "GITHUB_APP_ID",
        "GITHUB_APP_PRIVATE_KEY",
        "LANGFUSE_BASE_URL",
        "LANGFUSE_PROJECT_ID",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "NTFY_TOKEN",
        "NTFY_URL",
        "OPENROUTER_API_KEY",
        # -- Fields with no JSON entry (yet) — env-only or computed --
        "db_maintenance_interval_seconds",
        "db_maintenance_periodic",
        "deliver_max_identical_blocks",
        "investigation_workspace",
        "MILL_INVESTIGATION_WORKSPACE",
        "max_events_per_ticket",
        "refine_delta_reuse_enabled",
        "security_posture_interval_seconds",
        "security_posture_periodic",
        "security_posture_request_limit",
        "stage_timeout_overrides",
        "ticket_state_cycle_limit",
    }
)


# ---------------------------------------------------------------------------
#  Pure helpers
# ---------------------------------------------------------------------------


def build_valid_settings_names(model: type) -> set[str]:
    """Return the union of every ``model`` field name and non-null alias."""
    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        if field.alias:
            names.add(field.alias)
    return names


def check_json_settings_keys_in_model(
    json_keys: set[str], valid_names: set[str]
) -> list[str]:
    """Invariant 1: every JSON settings key must be a valid field name/alias."""
    return [
        f"JSON settings key not a Settings field name or alias: {key!r}"
        for key in sorted(json_keys - valid_names)
    ]


def check_model_fields_in_json(
    model: type,
    json_keys: set[str],
    exceptions: frozenset[str],
) -> list[str]:
    """Invariant 2: every Settings field must be represented in JSON
    ``settings:`` (by name or alias), unless explicitly excepted."""
    drift: list[str] = []
    for name, field in model.model_fields.items():
        if name in exceptions:
            continue
        if field.alias and field.alias in exceptions:
            continue
        if name in json_keys:
            continue
        if field.alias and field.alias in json_keys:
            continue
        drift.append(
            f"Settings field {name!r} (alias={field.alias!r}) has no entry"
            " in config.example.json settings and is not in the exception set"
        )
    return drift


def check_secrets_example(
    example_keys: set[str],
    secrets_fields: set[str],
) -> list[str]:
    """Invariants 3+4: secrets-block keys == Secrets fields."""
    drift: list[str] = []
    for key in sorted(example_keys - secrets_fields):
        drift.append(
            "config.example.json secrets key is not a Secrets field: "
            f"{key}"
        )
    for field in sorted(secrets_fields - example_keys):
        drift.append(
            f"Secrets field missing from config.example.json secrets block: "
            f"{field}"
        )
    return drift


def collect_drift() -> list[str]:
    """Load the real on-disk surfaces and run every invariant."""

    from robotsix_mill.config import Secrets, Settings

    with open(_CONFIG_EXAMPLE_JSON, "r", encoding="utf-8") as fh:
        config_example = json.load(fh)

    settings_json: dict = config_example.get("settings", {})
    secrets_json: dict = config_example.get("secrets", {})
    if not isinstance(settings_json, dict):
        settings_json = {}
    if not isinstance(secrets_json, dict):
        secrets_json = {}

    json_settings_keys: set[str] = set(settings_json)
    json_secrets_keys: set[str] = set(secrets_json)

    valid_settings_names = build_valid_settings_names(Settings)
    secrets_fields = set(Secrets.model_fields)

    drift: list[str] = []
    drift += check_json_settings_keys_in_model(json_settings_keys, valid_settings_names)
    drift += check_model_fields_in_json(
        Settings, json_settings_keys, _MODEL_FIELDS_NOT_IN_CONFIG
    )
    drift += check_secrets_example(json_secrets_keys, secrets_fields)
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

    print("config sync OK (JSON config, model fields, secrets all in sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
