#!/usr/bin/env python3
"""Deterministic config-surface drift checker for robotsix-mill.

Usage (from the repo root):
    python scripts/check_config_sync.py

Cross-references the live source-of-truth objects — never re-parses
source — to catch config drift that the heuristic ``config_sync`` LLM
agent would otherwise only notice on its next daily pass:

    * ``robotsix_mill.config.loader._YAML_PATH_TO_ALIAS`` — the
      hand-maintained dotted-YAML-path → Settings field/alias map.
    * ``robotsix_mill.config.Settings`` / ``Secrets`` — the Pydantic-v2
      models (introspected via ``model_fields``).
    * ``config/mill.defaults.yaml`` — the canonical defaults surface.
    * ``config/secrets.example.yaml`` — the secrets template.

Invariants (each contributes drift lines; the run fails if any fire):

    1. Every key of ``_YAML_PATH_TO_ALIAS`` resolves to a leaf path in
       ``config/mill.defaults.yaml``.
    2. Every leaf path in ``config/mill.defaults.yaml`` is a key of
       ``_YAML_PATH_TO_ALIAS``, except those listed in
       ``_DEFAULTS_KEYS_NOT_IN_ALIAS``.
    3. Every value of ``_YAML_PATH_TO_ALIAS`` is a real ``Settings``
       field name or field alias.
    4. The top-level keys of ``config/secrets.example.yaml`` equal the
       user-configurable ``Secrets`` fields, modulo
       ``_SECRETS_NOT_IN_EXAMPLE``.

This script is meant to be invoked from the repo root (which CI and the
``validate-config-sync`` pre-commit hook both guarantee).

Exit codes:
    0 — every invariant holds; the config surfaces are in sync.
    1 — at least one invariant fired; details are printed to stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Ensure both the repo root and src/ are importable so 'import
# robotsix_mill' works when run as a flat script (mirrors
# scripts/verify_repos_config.py).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_DEFAULTS_YAML = _REPO_ROOT / "config" / "mill.defaults.yaml"
_SECRETS_EXAMPLE_YAML = _REPO_ROOT / "config" / "secrets.example.yaml"


# ---------------------------------------------------------------------------
#  Explicit, commented exception sets
# ---------------------------------------------------------------------------

# Defaults-YAML leaf paths intentionally absent from
# ``_YAML_PATH_TO_ALIAS`` (invariant 2). Each entry documents where the
# value is actually consumed — it is NOT routed through the YAML→alias
# flatten flow.
_DEFAULTS_KEYS_NOT_IN_ALIAS: frozenset[str] = frozenset(
    {
        # Read directly by sandbox.py via Settings.sandbox_network /
        # Settings.sandbox_proxy_url; kept out of the flatten map (Docker
        # infrastructure wiring, not a per-run override surface).
        "sandbox.network",
        "sandbox.proxy_url",
    }
)

# ``Secrets`` fields that are intentionally NOT user-configurable via
# ``config/secrets.yaml`` (invariant 4), so they never appear in
# ``config/secrets.example.yaml``.
_SECRETS_NOT_IN_EXAMPLE: frozenset[str] = frozenset(
    {
        # Populated from RepoConfig (per-repo Langfuse project), never
        # from secrets.yaml — see docs/configuration.md.
        "langfuse_public_key",
        "langfuse_secret_key",
        "langfuse_base_url",
        "langfuse_project_id",
        "langfuse_project_name",
        # Non-secret host path; configured via
        # forge.github_app_private_key_path in the main config, not via
        # secrets.yaml.
        "github_app_private_key_path",
    }
)


# ---------------------------------------------------------------------------
#  Pure helpers (parameterised so synthetic cases need no monkeypatching)
# ---------------------------------------------------------------------------


def flatten_yaml_leaves(data: object, prefix: str = "") -> list[str]:
    """Return the dotted leaf paths of a nested YAML mapping.

    Any non-dict value is a leaf, **including an empty dict** (e.g.
    ``stage_timeout_overrides: {}``) and lists. A non-empty dict
    recurses into its children.
    """

    leaves: list[str] = []
    if isinstance(data, dict) and data:
        for key, value in data.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            leaves.extend(flatten_yaml_leaves(value, child))
    elif prefix:
        leaves.append(prefix)
    return leaves


def build_valid_settings_names(model: type) -> set[str]:
    """Return the union of every ``model`` field name and non-null alias."""

    names: set[str] = set()
    for name, field in model.model_fields.items():
        names.add(name)
        if field.alias:
            names.add(field.alias)
    return names


def check_map_keys_in_defaults(
    alias_map: dict[str, str], defaults_leaves: list[str]
) -> list[str]:
    """Invariant 1: every map key must be a defaults-YAML leaf path."""

    leaf_set = set(defaults_leaves)
    return [
        f"map key not found as a leaf in mill.defaults.yaml: {key}"
        for key in alias_map
        if key not in leaf_set
    ]


def check_defaults_leaves_in_map(
    defaults_leaves: list[str],
    alias_map: dict[str, str],
    exceptions: frozenset[str],
) -> list[str]:
    """Invariant 2: every defaults leaf must be a map key (or excepted)."""

    keys = set(alias_map)
    return [
        f"defaults leaf not mapped in _YAML_PATH_TO_ALIAS: {leaf}"
        for leaf in defaults_leaves
        if leaf not in keys and leaf not in exceptions
    ]


def check_map_values_resolve(
    alias_map: dict[str, str], valid_names: set[str]
) -> list[str]:
    """Invariant 3: every map value must be a real field name/alias."""

    return [
        f"map value {value!r} (for {key}) is not a Settings field name or alias"
        for key, value in alias_map.items()
        if value not in valid_names
    ]


def check_secrets_example(
    example_keys: set[str],
    secrets_fields: set[str],
    exceptions: frozenset[str],
) -> list[str]:
    """Invariant 4: secrets.example keys == user-configurable Secrets fields."""

    expected = secrets_fields - exceptions
    drift: list[str] = []
    for key in sorted(example_keys - expected):
        drift.append(
            f"secrets.example.yaml key is not a user-configurable Secrets field: {key}"
        )
    for field in sorted(expected - example_keys):
        drift.append(f"Secrets field missing from secrets.example.yaml: {field}")
    return drift


def collect_drift() -> list[str]:
    """Load the real on-disk surfaces and run every invariant."""

    from robotsix_mill.config import Secrets, Settings
    from robotsix_mill.config.loader import _YAML_PATH_TO_ALIAS

    with open(_DEFAULTS_YAML, "r", encoding="utf-8") as fh:
        defaults = yaml.safe_load(fh)
    with open(_SECRETS_EXAMPLE_YAML, "r", encoding="utf-8") as fh:
        secrets_example = yaml.safe_load(fh) or {}

    defaults_leaves = flatten_yaml_leaves(defaults)
    valid_names = build_valid_settings_names(Settings)
    secrets_fields = set(Secrets.model_fields)
    example_keys = set(secrets_example)

    drift: list[str] = []
    drift += check_map_keys_in_defaults(_YAML_PATH_TO_ALIAS, defaults_leaves)
    drift += check_defaults_leaves_in_map(
        defaults_leaves, _YAML_PATH_TO_ALIAS, _DEFAULTS_KEYS_NOT_IN_ALIAS
    )
    drift += check_map_values_resolve(_YAML_PATH_TO_ALIAS, valid_names)
    drift += check_secrets_example(
        example_keys, secrets_fields, _SECRETS_NOT_IN_EXAMPLE
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

    print("config sync OK (alias map, defaults YAML, secrets example all in sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
