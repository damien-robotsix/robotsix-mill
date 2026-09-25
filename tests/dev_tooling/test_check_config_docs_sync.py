"""Regression tests for scripts/check_config_docs_sync.py.

Covers:
    * Happy path against the real on-disk surfaces — zero drift across
      ``docs/config/configuration.md`` / the ``Settings`` model defaults.
    * Each deterministic invariant detects a synthetic violation when
      fed crafted inputs (no monkeypatching of imports — the pure
      functions take their inputs as parameters).
"""

from __future__ import annotations

from pathlib import Path

from tests.script_loader import load_script

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_config_docs_sync.py"

_checker = load_script(_SCRIPT_PATH)

collect_drift = _checker.collect_drift
_parse_doc_default = _checker._parse_doc_default
_python_default_to_doc_str = _checker._python_default_to_doc_str
_field_env_var = _checker._field_env_var
_build_valid_env_vars = _checker._build_valid_env_vars
_check_model_fields_in_docs = _checker._check_model_fields_in_docs
_check_doc_env_vars_in_model = _checker._check_doc_env_vars_in_model
_check_stale_no_doc_exceptions = _checker._check_stale_no_doc_exceptions


# ---------------------------------------------------------------------------
#  Happy path — real repo state
# ---------------------------------------------------------------------------


def test_real_repo_has_no_docs_sync_drift() -> None:
    drift = collect_drift()
    assert drift == [], f"config-docs-sync drift detected: {drift}"


# ---------------------------------------------------------------------------
#  _parse_doc_default — strip backtick-quoted defaults
# ---------------------------------------------------------------------------


def test_parse_doc_default_strips_outer_quotes() -> None:
    assert _parse_doc_default('"github.com"') == "github.com"


def test_parse_doc_default_leaves_bare_values() -> None:
    assert _parse_doc_default("true") == "true"
    assert _parse_doc_default("None") == "None"
    assert _parse_doc_default("42") == "42"


def test_parse_doc_default_empty_quotes() -> None:
    assert _parse_doc_default('""') == ""


# ---------------------------------------------------------------------------
#  _python_default_to_doc_str — Python value → doc-comparable string
# ---------------------------------------------------------------------------


def test_python_default_to_doc_str_none() -> None:
    assert _python_default_to_doc_str(None) == "None"


def test_python_default_to_doc_str_bool() -> None:
    assert _python_default_to_doc_str(True) == "true"
    assert _python_default_to_doc_str(False) == "false"


def test_python_default_to_doc_str_int() -> None:
    assert _python_default_to_doc_str(200000) == "200000"


def test_python_default_to_doc_str_str() -> None:
    assert _python_default_to_doc_str("some_value") == "some_value"


def test_python_default_to_doc_str_list() -> None:
    assert _python_default_to_doc_str(["a", "b"]) == '["a", "b"]'


def test_python_default_to_doc_str_path() -> None:
    assert _python_default_to_doc_str(Path("/tmp/foo")) == "/tmp/foo"


# ---------------------------------------------------------------------------
#  _field_env_var — alias vs derived env-var name
# ---------------------------------------------------------------------------


def test_field_env_var_uses_alias_when_present() -> None:
    from pydantic import BaseModel, Field

    class M(BaseModel):
        env_field: int = Field(default=0, alias="MILL_CUSTOM")

    field = M.model_fields["env_field"]
    assert _field_env_var("env_field", field) == "MILL_CUSTOM"


def test_field_env_var_derives_from_name_when_no_alias() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        model: str = ""

    field = M.model_fields["model"]
    # alias is None → derived: MILL_ + uppercase name
    assert field.alias is None
    assert _field_env_var("model", field) == "MILL_MODEL"


# ---------------------------------------------------------------------------
#  _build_valid_env_vars
# ---------------------------------------------------------------------------


def test_build_valid_env_vars_collects_all() -> None:
    from pydantic import BaseModel, Field

    class M(BaseModel):
        a: str = ""
        b: int = Field(default=0, alias="MILL_B_CUSTOM")

    names = _build_valid_env_vars(M)
    assert "MILL_A" in names
    assert "MILL_B_CUSTOM" in names
    assert "MILL_B" not in names


# ---------------------------------------------------------------------------
#  _check_model_fields_in_docs — field default must be documented
# ---------------------------------------------------------------------------


def test_model_fields_in_docs_detects_missing_doc_entry() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        known: str = "val"

    doc_defaults: dict[str, str] = {}  # no doc entries
    drift = _check_model_fields_in_docs(M, doc_defaults, exceptions=frozenset())
    assert len(drift) == 1
    assert "known" in drift[0]
    assert "MILL_KNOWN" in drift[0]


def test_model_fields_in_docs_detects_default_mismatch() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        timeout: int = 30

    doc_defaults = {"MILL_TIMEOUT": "60"}
    drift = _check_model_fields_in_docs(M, doc_defaults, exceptions=frozenset())
    assert len(drift) == 1
    assert "timeout" in drift[0]
    assert "30" in drift[0]
    assert "60" in drift[0] or '"60"' in drift[0]


def test_model_fields_in_docs_skips_excepted_fields() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        orphan: int = 0
        known: str = ""

    doc_defaults = {"MILL_KNOWN": ""}
    drift = _check_model_fields_in_docs(
        M, doc_defaults, exceptions=frozenset({"orphan"})
    )
    assert drift == []


def test_model_fields_in_docs_skips_undefined_default() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        required: str  # no default

    doc_defaults: dict[str, str] = {}
    drift = _check_model_fields_in_docs(M, doc_defaults, exceptions=frozenset())
    assert drift == []


# ---------------------------------------------------------------------------
#  _check_doc_env_vars_in_model — doc env var must map to a real field
# ---------------------------------------------------------------------------


def test_doc_env_vars_in_model_detects_stray_doc_entry() -> None:
    doc_defaults = {"MILL_KNOWN": "val", "MILL_STRAY": "x"}
    valid_env_vars = {"MILL_KNOWN"}
    drift = _check_doc_env_vars_in_model(
        doc_defaults, valid_env_vars, exceptions=frozenset()
    )
    assert len(drift) == 1
    assert "MILL_STRAY" in drift[0]


def test_doc_env_vars_in_model_respects_exceptions() -> None:
    doc_defaults = {"MILL_KNOWN": "val", "MILL_INTENTIONAL": "x"}
    valid_env_vars = {"MILL_KNOWN"}
    drift = _check_doc_env_vars_in_model(
        doc_defaults, valid_env_vars, exceptions=frozenset({"MILL_INTENTIONAL"})
    )
    assert drift == []


# ---------------------------------------------------------------------------
#  _check_stale_no_doc_exceptions — exception entries that now have docs
# ---------------------------------------------------------------------------


def test_stale_no_doc_exceptions_detects_stale_entry() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        my_field: str = ""

    doc_defaults = {"MILL_MY_FIELD": "some default"}
    drift = _check_stale_no_doc_exceptions(
        M, doc_defaults, exceptions=frozenset({"my_field"})
    )
    assert len(drift) == 1
    assert "my_field" in drift[0]
    assert "MILL_MY_FIELD" in drift[0]


def test_stale_no_doc_exceptions_skips_absent_field() -> None:
    from pydantic import BaseModel

    class M(BaseModel):
        pass

    doc_defaults: dict[str, str] = {}
    drift = _check_stale_no_doc_exceptions(
        M, doc_defaults, exceptions=frozenset({"deleted_field"})
    )
    assert drift == []
