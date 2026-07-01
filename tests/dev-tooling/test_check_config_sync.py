"""Regression tests for scripts/check_config_sync.py.

Covers:
    * Happy path against the real on-disk surfaces — zero drift across
      ``_YAML_PATH_TO_ALIAS`` / ``config/config.yaml`` (its
      non-secret leaves + ``secrets:`` block) / the ``Settings`` &
      ``Secrets`` models.
    * Each deterministic invariant detects a synthetic violation when
      fed crafted inputs (no monkeypatching of imports — the pure
      functions take their inputs as parameters).
    * The leaf flattener treats an empty dict and lists as leaves.
"""

from __future__ import annotations

from pathlib import Path

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_config_sync.py"

_checker = load_script(_SCRIPT_PATH)

flatten_yaml_leaves = _checker.flatten_yaml_leaves
build_valid_settings_names = _checker.build_valid_settings_names
check_map_keys_in_defaults = _checker.check_map_keys_in_defaults
check_defaults_leaves_in_map = _checker.check_defaults_leaves_in_map
check_map_values_resolve = _checker.check_map_values_resolve
check_secrets_example = _checker.check_secrets_example
check_model_fields_in_alias_map = _checker.check_model_fields_in_alias_map
collect_drift = _checker.collect_drift


# ---------------------------------------------------------------------------
#  Happy path — real repo state
# ---------------------------------------------------------------------------


def test_real_repo_has_no_config_drift() -> None:
    drift = collect_drift()
    assert drift == [], f"config-sync drift detected: {drift}"


# ---------------------------------------------------------------------------
#  Leaf flattener semantics
# ---------------------------------------------------------------------------


def test_flatten_treats_empty_dict_and_list_as_leaves() -> None:
    leaves = flatten_yaml_leaves(
        {
            "a": {"b": {"c": 1}},
            "empty": {},
            "items": [1, 2, 3],
            "scalar": "x",
            "nothing": None,
        }
    )
    assert set(leaves) == {"a.b.c", "empty", "items", "scalar", "nothing"}


# ---------------------------------------------------------------------------
#  Invariant 1 — map key must be a defaults-YAML leaf
# ---------------------------------------------------------------------------


def test_invariant1_detects_bogus_map_key() -> None:
    defaults_leaves = ["core.models.coordinator", "service.api_port"]
    alias_map = {
        "core.models.coordinator": "model",
        "core.bogus_key": "whatever",
    }
    drift = check_map_keys_in_defaults(alias_map, defaults_leaves)
    assert any("core.bogus_key" in entry for entry in drift)
    assert not any("core.models.coordinator" in entry for entry in drift)


# ---------------------------------------------------------------------------
#  Invariant 2 — defaults leaf must be mapped (or excepted)
# ---------------------------------------------------------------------------


def test_invariant2_detects_unmapped_defaults_leaf() -> None:
    defaults_leaves = ["core.models.coordinator", "core.synthetic_orphan"]
    alias_map = {"core.models.coordinator": "model"}
    drift = check_defaults_leaves_in_map(
        defaults_leaves, alias_map, exceptions=frozenset()
    )
    assert any("core.synthetic_orphan" in entry for entry in drift)


def test_invariant2_respects_exceptions() -> None:
    defaults_leaves = ["sandbox.network"]
    alias_map: dict[str, str] = {}
    drift = check_defaults_leaves_in_map(
        defaults_leaves, alias_map, exceptions=frozenset({"sandbox.network"})
    )
    assert drift == []


# ---------------------------------------------------------------------------
#  Invariant 3 — map value must resolve to a real field name/alias
# ---------------------------------------------------------------------------


def test_invariant3_detects_unresolved_map_value() -> None:
    valid_names = {"model", "explore_model"}
    alias_map = {
        "core.models.coordinator": "model",
        "core.models.ghost": "no_such_field",
    }
    drift = check_map_values_resolve(alias_map, valid_names)
    assert any(
        "no_such_field" in entry and "core.models.ghost" in entry for entry in drift
    )
    assert not any("'model'" in entry for entry in drift)


# ---------------------------------------------------------------------------
#  Invariant 4 — secrets example must equal user-configurable fields
# ---------------------------------------------------------------------------


def test_invariant4_detects_example_key_absent_from_model() -> None:
    secrets_fields = {"openrouter_api_key", "forge_token"}
    example_keys = {"openrouter_api_key", "forge_token", "stray_key"}
    drift = check_secrets_example(example_keys, secrets_fields, exceptions=frozenset())
    assert any("stray_key" in entry for entry in drift)


def test_invariant4_detects_field_missing_from_example() -> None:
    secrets_fields = {"openrouter_api_key", "forge_token"}
    example_keys = {"openrouter_api_key"}
    drift = check_secrets_example(example_keys, secrets_fields, exceptions=frozenset())
    assert any("forge_token" in entry for entry in drift)


def test_invariant4_respects_exceptions() -> None:
    secrets_fields = {"openrouter_api_key", "langfuse_public_key"}
    example_keys = {"openrouter_api_key"}
    drift = check_secrets_example(
        example_keys, secrets_fields, exceptions=frozenset({"langfuse_public_key"})
    )
    assert drift == []


# ---------------------------------------------------------------------------
#  build_valid_settings_names — union of field names and aliases
# ---------------------------------------------------------------------------


def test_valid_names_include_field_names_and_aliases() -> None:
    from robotsix_mill.config import Settings

    names = build_valid_settings_names(Settings)
    # Field name with no alias.
    assert "claude_sdk_vision_enabled" in names
    # Alias-bearing field exposes both the name and the alias.
    assert "FORGE_KIND" in names


# ---------------------------------------------------------------------------
#  Invariant 5 — Settings field must be in alias-map values (or excepted)
# ---------------------------------------------------------------------------


def test_invariant5_detects_model_field_not_in_alias() -> None:
    """A model field not in alias values and not excepted fires drift."""
    from pydantic import BaseModel

    class M(BaseModel):
        known: str = ""
        orphan: int = 0

    alias_map = {"some.path": "known"}
    drift = check_model_fields_in_alias_map(M, alias_map, exceptions=frozenset())
    assert len(drift) == 1
    assert "orphan" in drift[0]
    assert "known" not in drift[0]


def test_invariant5_respects_exceptions() -> None:
    """A model field in the exception set is skipped."""
    from pydantic import BaseModel

    class M(BaseModel):
        known: str = ""
        orphan: int = 0

    alias_map = {"some.path": "known"}
    drift = check_model_fields_in_alias_map(
        M, alias_map, exceptions=frozenset({"orphan"})
    )
    assert drift == []


def test_invariant5_matches_field_alias() -> None:
    """A field whose alias (not name) matches an alias value passes."""
    from pydantic import BaseModel, Field

    class M(BaseModel):
        known: str = ""
        env_field: int = Field(default=0, alias="ENV_FIELD")

    alias_map = {"some.path": "known", "other.path": "ENV_FIELD"}
    drift = check_model_fields_in_alias_map(M, alias_map, exceptions=frozenset())
    assert drift == []


def test_invariant5_empty_when_all_covered() -> None:
    """No drift when every field is either in alias or excepted."""
    from pydantic import BaseModel

    class M(BaseModel):
        a: str = ""
        b: str = ""

    alias_map = {"x.a": "a"}
    drift = check_model_fields_in_alias_map(M, alias_map, exceptions=frozenset({"b"}))
    assert drift == []
