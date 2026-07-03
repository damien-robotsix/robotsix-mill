"""Regression tests for scripts/check_config_sync.py.

Covers:
    * Happy path against the real on-disk surfaces — zero drift across
      the JSON config / ``config/config.example.json`` (its
      ``settings:`` + ``secrets:`` blocks) / the ``Settings`` &
      ``Secrets`` models.
    * Each deterministic invariant detects a synthetic violation when
      fed crafted inputs (no monkeypatching of imports — the pure
      functions take their inputs as parameters).
"""

from __future__ import annotations

from pathlib import Path

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_config_sync.py"

_checker = load_script(_SCRIPT_PATH)

build_valid_settings_names = _checker.build_valid_settings_names
check_json_settings_keys_in_model = _checker.check_json_settings_keys_in_model
check_model_fields_in_json = _checker.check_model_fields_in_json
check_secrets_example = _checker.check_secrets_example
collect_drift = _checker.collect_drift


# ---------------------------------------------------------------------------
#  Happy path — real repo state
# ---------------------------------------------------------------------------


def test_real_repo_has_no_config_drift() -> None:
    drift = collect_drift()
    assert drift == [], f"config-sync drift detected: {drift}"


# ---------------------------------------------------------------------------
#  Invariant 1 — every JSON settings key must be a valid field name/alias
# ---------------------------------------------------------------------------


def test_invariant1_detects_unknown_json_key() -> None:
    valid_names = {"model", "FORGE_KIND"}
    json_keys = {"model", "FORGE_KIND", "bogus_key"}
    drift = check_json_settings_keys_in_model(json_keys, valid_names)
    assert any("bogus_key" in entry for entry in drift)
    assert not any("model" in entry or "FORGE_KIND" in entry for entry in drift)


def test_invariant1_passes_when_all_keys_valid() -> None:
    valid_names = {"model", "FORGE_KIND"}
    json_keys = {"model", "FORGE_KIND"}
    drift = check_json_settings_keys_in_model(json_keys, valid_names)
    assert drift == []


# ---------------------------------------------------------------------------
#  Invariant 2 — every Settings field must be in JSON (or excepted)
# ---------------------------------------------------------------------------


def test_invariant2_detects_missing_field() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        known: str = ""
        orphan: int = 0

    json_keys = {"known"}
    drift = check_model_fields_in_json(M, json_keys, exceptions=frozenset())
    assert len(drift) == 1
    assert "orphan" in drift[0]
    assert "known" not in drift[0]


def test_invariant2_respects_exceptions() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        known: str = ""
        orphan: int = 0

    json_keys = {"known"}
    drift = check_model_fields_in_json(M, json_keys, exceptions=frozenset({"orphan"}))
    assert drift == []


def test_invariant2_matches_field_alias() -> None:
    """A field whose alias (not name) is in JSON passes."""
    from pydantic import BaseModel, Field

    class M(BaseModel):
        known: str = ""
        env_field: int = Field(default=0, alias="ENV_FIELD")

    json_keys = {"known", "ENV_FIELD"}
    drift = check_model_fields_in_json(M, json_keys, exceptions=frozenset())
    assert drift == []


def test_invariant2_empty_when_all_covered() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        a: str = ""
        b: str = ""

    json_keys = {"a"}
    drift = check_model_fields_in_json(M, json_keys, exceptions=frozenset({"b"}))
    assert drift == []


# ---------------------------------------------------------------------------
#  Invariant 3 — secrets example must equal Secrets fields
# ---------------------------------------------------------------------------


def test_invariant3_detects_example_key_absent_from_model() -> None:
    secrets_fields = {"openrouter_api_key", "forge_token"}
    example_keys = {"openrouter_api_key", "forge_token", "stray_key"}
    drift = check_secrets_example(example_keys, secrets_fields)
    assert any("stray_key" in entry for entry in drift)


def test_invariant3_detects_field_missing_from_example() -> None:
    secrets_fields = {"openrouter_api_key", "forge_token"}
    example_keys = {"openrouter_api_key"}
    drift = check_secrets_example(example_keys, secrets_fields)
    assert any("forge_token" in entry for entry in drift)


def test_invariant3_passes_when_in_sync() -> None:
    secrets_fields = {"openrouter_api_key", "forge_token"}
    example_keys = {"openrouter_api_key", "forge_token"}
    drift = check_secrets_example(example_keys, secrets_fields)
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
