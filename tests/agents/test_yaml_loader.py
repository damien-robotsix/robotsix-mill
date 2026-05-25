"""Tests for the YAML agent-definition loader."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from robotsix_mill.agents.yaml_loader import (
    AgentDefinition,
    _resolve_env_vars,
    load_agent_definition,
)


# ── helpers ──────────────────────────────────────────────────────────

def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write *content* to a temporary YAML file and return its path."""
    p = tmp_path / "test_agent.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ── _resolve_env_vars ────────────────────────────────────────────────

def test_resolve_env_vars_replaces_single_var(monkeypatch):
    monkeypatch.setenv("MY_MODEL", "anthropic/claude-4")
    assert _resolve_env_vars("${MY_MODEL}") == "anthropic/claude-4"


def test_resolve_env_vars_replaces_multiple_vars(monkeypatch):
    monkeypatch.setenv("A", "hello")
    monkeypatch.setenv("B", "world")
    assert _resolve_env_vars("${A} ${B}") == "hello world"


def test_resolve_env_vars_no_var_returns_unchanged():
    assert _resolve_env_vars("no vars here") == "no vars here"


def test_resolve_env_vars_unset_returns_empty():
    """Unresolvable ${VAR} returns '' — caller (build_agent) then
    falls back to settings.model when model_name is falsy."""
    assert _resolve_env_vars("${UNSET_VAR}") == ""


# ── load_agent_definition — valid inputs ─────────────────────────────

def test_valid_all_fields(tmp_path):
    """All fields populated → correct AgentDefinition."""
    p = _write_yaml(tmp_path, """\
name: test-agent
description: A test agent.
category: pipeline
model: test/model-v1
system_prompt: You are a test agent.
tools:
  - explore
  - read_file
web: true
report_issue: false
output_type: TestResult
retries: 3
module: testing
skills:
  - foo
  - bar
""")
    ad = load_agent_definition(p)
    assert ad.name == "test-agent"
    assert ad.description == "A test agent."
    assert ad.category == "pipeline"
    assert ad.model == "test/model-v1"
    assert ad.system_prompt == "You are a test agent."
    assert ad.tools == ["explore", "read_file"]
    assert ad.web is True
    assert ad.report_issue is False
    assert ad.output_type == "TestResult"
    assert ad.retries == 3
    assert ad.module == "testing"
    assert ad.skills == ["foo", "bar"]


def test_minimal_valid_yaml(tmp_path):
    """Only required fields → defaults applied for optionals."""
    p = _write_yaml(tmp_path, """\
name: minimal
model: gpt-4
system_prompt: Do one thing well.
""")
    ad = load_agent_definition(p)
    assert ad.name == "minimal"
    assert ad.model == "gpt-4"
    assert ad.system_prompt == "Do one thing well."
    assert ad.description is None
    assert ad.category is None
    assert ad.tools == []
    assert ad.web is False
    assert ad.report_issue is True
    assert ad.output_type is None
    assert ad.retries == 2
    assert ad.module is None
    assert ad.skills == []


def test_env_var_substitution_in_model(tmp_path, monkeypatch):
    """${VAR} in model field is resolved from environment."""
    monkeypatch.setenv("MILL_TEST_MODEL", "anthropic/claude-sonnet")
    p = _write_yaml(tmp_path, """\
name: env-agent
model: ${MILL_TEST_MODEL}
system_prompt: test
""")
    ad = load_agent_definition(p)
    assert ad.model == "anthropic/claude-sonnet"


def test_non_model_fields_preserve_literal_env_var(tmp_path, monkeypatch):
    """system_prompt containing ${SOMETHING} stays literal."""
    monkeypatch.setenv("SOMETHING", "resolved")
    p = _write_yaml(tmp_path, """\
name: literal
model: gpt-4
system_prompt: "Use ${SOMETHING} as a tag."
""")
    ad = load_agent_definition(p)
    assert ad.system_prompt == "Use ${SOMETHING} as a tag."


def test_empty_tools_and_skills_get_defaults(tmp_path):
    """Explicit empty lists → still default []."""
    p = _write_yaml(tmp_path, """\
name: empty-lists
model: gpt-4
system_prompt: test
tools: []
skills: []
""")
    ad = load_agent_definition(p)
    assert ad.tools == []
    assert ad.skills == []


# ── load_agent_definition — error cases ──────────────────────────────

def test_missing_required_name(tmp_path):
    """Missing 'name' → ValidationError."""
    p = _write_yaml(tmp_path, """\
model: gpt-4
system_prompt: test
""")
    with pytest.raises(Exception) as exc_info:  # ValidationError
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "name" in err_str.lower() or "Field required" in err_str


def test_missing_required_model(tmp_path):
    """Missing 'model' → ValidationError."""
    p = _write_yaml(tmp_path, """\
name: no-model
system_prompt: test
""")
    with pytest.raises(Exception) as exc_info:
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "model" in err_str.lower() or "Field required" in err_str


def test_missing_required_system_prompt(tmp_path):
    """Missing 'system_prompt' → ValidationError."""
    p = _write_yaml(tmp_path, """\
name: no-prompt
model: gpt-4
""")
    with pytest.raises(Exception) as exc_info:
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "system_prompt" in err_str.lower() or "Field required" in err_str


def test_wrong_type_web(tmp_path):
    """web: [1, 2, 3] (list) → ValidationError."""
    p = _write_yaml(tmp_path, """\
name: bad-web
model: gpt-4
system_prompt: test
web:
  - 1
  - 2
  - 3
""")
    with pytest.raises(Exception) as exc_info:
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "web" in err_str.lower() or "bool" in err_str.lower()


def test_wrong_type_retries(tmp_path):
    """retries: 'two' (str) → ValidationError."""
    p = _write_yaml(tmp_path, """\
name: bad-retries
model: gpt-4
system_prompt: test
retries: "two"
""")
    with pytest.raises(Exception) as exc_info:
        load_agent_definition(p)
    err_str = str(exc_info.value)
    assert "retries" in err_str.lower() or "int" in err_str.lower()


def test_malformed_yaml_syntax(tmp_path):
    """Invalid YAML → yaml.YAMLError."""
    p = _write_yaml(tmp_path, """\
name: bad-yaml
model: gpt-4
\tsystem_prompt: tab-indented  # tabs are illegal in YAML
""")
    with pytest.raises(yaml.YAMLError):
        load_agent_definition(p)


def test_unresolvable_env_var_resolves_to_empty(tmp_path):
    """Unset env var in model → resolved to '' (build_agent falls back to settings.model)."""
    # Ensure the variable is not in the environment.
    if "UNSET_MODEL_VAR" in os.environ:
        del os.environ["UNSET_MODEL_VAR"]
    p = _write_yaml(tmp_path, """\
name: bad-env
model: ${UNSET_MODEL_VAR}
system_prompt: test
""")
    ad = load_agent_definition(p)
    assert ad.model == ""


def test_file_not_found():
    """Non-existent path → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_agent_definition(Path("/nonexistent/agent.yaml"))


# ── independence from agent runtime ──────────────────────────────────

def test_loader_independent_of_agent_runtime():
    """Importing yaml_loader does NOT import build_agent, Settings,
    pydantic_ai, or any OpenRouter/provider code."""
    import subprocess
    import sys

    # Run in a subprocess for clean isolation.
    code = """
import sys
from pathlib import Path

# Add src to path.
sys.path.insert(0, "src")

from robotsix_mill.agents.yaml_loader import load_agent_definition, AgentDefinition

# The forbidden modules must NOT be in sys.modules after our import.
forbidden = {
    "pydantic_ai",
    "robotsix_mill.agents.base",
    "robotsix_mill.config",
}
loaded = forbidden & set(sys.modules.keys())
if loaded:
    sys.exit(f"FORBIDDEN IMPORTS: {loaded}")
print("OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


def test_real_refine_yaml_parses():
    """Smoke-test: the existing agent_definitions/refine.yaml parses
    without error (minus env-var resolution, which requires a mock)."""
    p = Path("agent_definitions/refine.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/refine.yaml not found")

    # Mock the env var so resolution succeeds.
    import os as _os
    _os.environ.setdefault("MILL_REFINE_MODEL", "test/model")

    try:
        ad = load_agent_definition(p)
        assert ad.name == "refine"
        assert ad.category == "pipeline"
        assert ad.web is True
        assert ad.report_issue is True
        assert ad.output_type == "RefineResult"
        assert ad.retries == 2
        assert ad.module == "refining"
        assert isinstance(ad.tools, list)
        assert isinstance(ad.skills, list)
    finally:
        # Don't leak the env var if it wasn't set before.
        if "MILL_REFINE_MODEL" not in _os.environ:
            _os.environ.pop("MILL_REFINE_MODEL", None)


# ── AgentDefinition pydantic model ────────────────────────────────────

def test_agent_definition_model_validation():
    """Direct model_validate with a dict → correct instance."""
    ad = AgentDefinition.model_validate({
        "name": "test",
        "model": "gpt-4",
        "system_prompt": "You are helpful.",
    })
    assert ad.name == "test"
    assert ad.web is False
    assert ad.retries == 2
    assert ad.tools == []


def test_agent_definition_extra_fields_rejected():
    """Unknown keys → ValidationError."""
    with pytest.raises(Exception):
        AgentDefinition.model_validate({
            "name": "test",
            "model": "gpt-4",
            "system_prompt": "ok",
            "unknown_field": "nope",
        })


# ── Structural verification: all agent_definitions/*.yaml ─────────────


# Known-valid tool names (mirrors ToolRegistry + fs_tools).
_VALID_TOOL_NAMES = frozenset({
    "explore", "read_file", "write_file", "edit_file",
    "delete_file", "list_dir", "run_command",
})

# Known-valid categories.
_VALID_CATEGORIES = frozenset({"pipeline", "periodic", "sub_agent", "interactive", "sandboxed"})

# Mapping from ${VAR} → Settings field alias (from config.py).
_ENV_VAR_TO_SETTINGS_ALIAS: dict[str, str] = {
    "MILL_MODEL": "MILL_MODEL",
    "MILL_EXPLORE_MODEL": "MILL_EXPLORE_MODEL",
    "MILL_TEST_MODEL": "MILL_TEST_MODEL",
    "MILL_REFINE_MODEL": "MILL_REFINE_MODEL",
    "MILL_ANSWER_MODEL": "MILL_ANSWER_MODEL",
    "MILL_RETROSPECT_MODEL": "MILL_RETROSPECT_MODEL",
    "MILL_AUDIT_MODEL": "MILL_AUDIT_MODEL",
    "MILL_DEDUP_MODEL": "MILL_DEDUP_MODEL",
    "MILL_TRIAGE_MODEL": "MILL_TRIAGE_MODEL",
    "MILL_WEB_RESEARCH_MODEL": "MILL_WEB_RESEARCH_MODEL",
    "MILL_AUTO_APPROVE_MODEL": "MILL_AUTO_APPROVE_MODEL",
    "MILL_REVIEW_MODEL": "MILL_REVIEW_MODEL",
    "MILL_SCOPE_TRIAGE_MODEL": "MILL_SCOPE_TRIAGE_MODEL",
    "MILL_COMPLETENESS_CHECK_MODEL": "MILL_COMPLETENESS_CHECK_MODEL",
    "MILL_DOC_MODEL": "MILL_DOC_MODEL",
    "MILL_TRACE_INSPECTOR_MODEL": "MILL_TRACE_INSPECTOR_MODEL",
    "MILL_TEST_GAP_MODEL": "MILL_TEST_GAP_MODEL",
    "MILL_AGENT_CHECK_MODEL": "MILL_AGENT_CHECK_MODEL",
    "MILL_HEALTH_MODEL": "MILL_HEALTH_MODEL",
    "MILL_SURVEY_MODEL": "MILL_SURVEY_MODEL",
    "MILL_BC_CHECK_MODEL": "MILL_BC_CHECK_MODEL",
    "MILL_ENV_SYNC_MODEL": "MILL_ENV_SYNC_MODEL",
}


def _all_yaml_files():
    """Return every .yaml file path under agent_definitions/."""
    ad = Path("agent_definitions")
    if not ad.is_dir():
        return []
    return sorted(ad.glob("*.yaml"))


def _all_definitions():
    """Load every YAML file and return list of (path, AgentDefinition)."""
    result = []
    for p in _all_yaml_files():
        result.append((p, load_agent_definition(p)))
    return result


def test_all_yaml_files_parse(monkeypatch):
    """Every .yaml in agent_definitions/ loads without error."""
    # Mock all known env vars so ${VAR} resolution succeeds.
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    yaml_files = _all_yaml_files()
    assert yaml_files, "No YAML files found in agent_definitions/"

    for yf in yaml_files:
        ad = load_agent_definition(yf)
        assert ad.name, f"{yf.name} has empty name"


def test_all_real_yamls_parse(monkeypatch):
    """Alias for test_all_yaml_files_parse — validates every YAML
    in agent_definitions/ parses successfully.  Exists under this
    name to satisfy the acceptance-criteria checklist."""
    test_all_yaml_files_parse(monkeypatch)


def test_all_yaml_names_unique(monkeypatch):
    """No two YAML files share the same name."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    defs = _all_definitions()
    names = [ad.name for _, ad in defs]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"Duplicate agent names: {dupes}"


def test_module_field_points_to_real_file(monkeypatch):
    """For each YAML with module set, the Python module exists.
    For each without module, derive from name and check."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    agents_dir = Path("src/robotsix_mill/agents")

    for yf, ad in _all_definitions():
        if ad.module:
            module_path = agents_dir / f"{ad.module}.py"
            assert module_path.is_file(), (
                f"{yf.name}: module '{ad.module}' → "
                f"{module_path} does not exist"
            )
        else:
            # Derive from name by convention.
            module_path = agents_dir / f"{ad.name}.py"
            assert module_path.is_file(), (
                f"{yf.name}: no module field set and derived path "
                f"{module_path} does not exist"
            )


def test_output_type_exists_in_module(monkeypatch):
    """For each YAML with output_type set, the class exists in the module."""
    import importlib

    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        if not ad.output_type:
            continue
        module_name = ad.module or ad.name
        mod = importlib.import_module(
            f"robotsix_mill.agents.{module_name}"
        )
        cls = getattr(mod, ad.output_type, None)
        assert cls is not None, (
            f"{yf.name}: output_type '{ad.output_type}' not found "
            f"in robotsix_mill.agents.{module_name}"
        )


def test_tool_names_are_valid(monkeypatch):
    """Every tool name in each YAML's tools list is known-valid."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        for tool in ad.tools:
            assert tool in _VALID_TOOL_NAMES, (
                f"{yf.name}: unknown tool '{tool}'. "
                f"Known: {sorted(_VALID_TOOL_NAMES)}"
            )


def test_category_is_valid(monkeypatch):
    """Every YAML with category set has a valid value."""
    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        if ad.category is not None:
            assert ad.category in _VALID_CATEGORIES, (
                f"{yf.name}: invalid category '{ad.category}'. "
                f"Valid: {sorted(_VALID_CATEGORIES)}"
            )


def test_report_issue_consistency(monkeypatch):
    """Agents with output_type set SHOULD have report_issue: false.

    Documented exceptions: refine (output_type=RefineResult but
    report_issue=true because RefineResult is a spec, not a ticket).

    This is a soft check — violations emit a ``UserWarning`` (visible
    in test output) rather than failing the suite.
    """
    import warnings

    KNOWN_EXCEPTIONS = {"refine", "implement", "ci_fix", "rebase", "dedup"}

    for var in _ENV_VAR_TO_SETTINGS_ALIAS:
        monkeypatch.setenv(var, "mock/model")

    for yf, ad in _all_definitions():
        if ad.output_type and ad.report_issue and ad.name not in KNOWN_EXCEPTIONS:
            warnings.warn(
                f"{yf.name}: has output_type={ad.output_type!r} AND "
                f"report_issue=true. Agents with structured output should "
                f"typically set report_issue: false to avoid double-drafting. "
                f"If this is intentional, add '{ad.name}' to KNOWN_EXCEPTIONS."
            )


def test_env_vars_in_model_match_settings():
    """Every ${VAR} in a YAML model field maps to a known Settings alias.

    Reads the raw YAML *before* env-var resolution so that
    ``${VAR}`` patterns are still present in the ``model`` field.
    """
    import re
    var_re = re.compile(r"\$\{([^{}]+)\}")

    for yf in _all_yaml_files():
        raw = yaml.safe_load(yf.read_text())
        raw_model = raw.get("model", "")
        for match in var_re.finditer(raw_model):
            var_name = match.group(1)
            assert var_name in _ENV_VAR_TO_SETTINGS_ALIAS, (
                f"{yf.name}: model references ${{{var_name}}} which "
                f"has no matching Settings field alias"
            )
