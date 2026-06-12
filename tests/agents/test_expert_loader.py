"""Tests for the YAML expert-definition loader."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pydantic import ValidationError

from robotsix_mill.agents.expert_loader import (
    ExpertDefinition,
    ExpertMemoryConfig,
    _resolve_env_vars,
    load_expert_definition,
)


# ── helpers ──────────────────────────────────────────────────────────


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """Write *content* to a temporary YAML file and return its path."""
    p = tmp_path / "test_expert.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ── _resolve_env_vars ────────────────────────────────────────────────


def test_resolve_env_vars_replaces_single_var(monkeypatch):
    monkeypatch.setenv("MY_EXPERT_MODEL", "anthropic/claude-4")
    assert _resolve_env_vars("${MY_EXPERT_MODEL}") == "anthropic/claude-4"


def test_resolve_env_vars_replaces_multiple_vars(monkeypatch):
    monkeypatch.setenv("A", "hello")
    monkeypatch.setenv("B", "world")
    assert _resolve_env_vars("${A} ${B}") == "hello world"


def test_resolve_env_vars_no_var_returns_unchanged():
    assert _resolve_env_vars("no vars here") == "no vars here"


def test_resolve_env_vars_unset_returns_empty():
    """Unresolvable ${VAR} returns '' — caller (ExpertManager) then
    falls back to the global expert model when model is falsy."""
    assert _resolve_env_vars("${UNSET_VAR}") == ""


# ── ExpertMemoryConfig ───────────────────────────────────────────────


def test_memory_config_defaults():
    """All defaults are applied when constructing with no args."""
    mc = ExpertMemoryConfig()
    assert mc.max_memory_chars == 8000
    assert mc.chunk_size == 2000
    assert mc.max_chunks == 20
    assert mc.memory_path is None
    assert mc.extras == {}


def test_memory_config_partial_overrides():
    """Explicit values override defaults; others stay."""
    mc = ExpertMemoryConfig(chunk_size=4000)
    assert mc.max_memory_chars == 8000  # default
    assert mc.chunk_size == 4000
    assert mc.max_chunks == 20  # default
    assert mc.memory_path is None  # default
    assert mc.extras == {}  # default


def test_memory_config_extra_forbid():
    """Unknown keys are rejected."""
    with pytest.raises(ValidationError):
        ExpertMemoryConfig.model_validate(
            {
                "max_memory_chars": 8000,
                "unknown_key": "nope",
            }
        )


# ── ExpertDefinition model ────────────────────────────────────────────


def test_expert_definition_model_validation():
    """Direct model_validate with minimal dict → correct instance."""
    ed = ExpertDefinition.model_validate(
        {
            "domain": "test-domain",
            "module_paths": ["src/**/*.py"],
            "system_prompt": "You are a test expert.",
        }
    )
    assert ed.domain == "test-domain"
    assert ed.description is None
    assert ed.module_paths == ["src/**/*.py"]
    assert ed.system_prompt == "You are a test expert."
    assert ed.model == ""
    assert isinstance(ed.memory, ExpertMemoryConfig)
    assert ed.memory.max_memory_chars == 8000
    assert ed.skills == []
    assert ed.tools == ["explore", "read_file", "list_dir"]
    assert ed.extras == {}


def test_expert_definition_extra_fields_rejected():
    """Unknown top-level keys → ValidationError."""
    with pytest.raises(ValidationError):
        ExpertDefinition.model_validate(
            {
                "domain": "test-domain",
                "module_paths": ["src/**/*.py"],
                "system_prompt": "ok",
                "unknown_field": "nope",
            }
        )


def test_expert_definition_default_tools():
    """Default tools is read-only exploration tools, not empty list."""
    ed = ExpertDefinition.model_validate(
        {
            "domain": "test-domain",
            "module_paths": ["src/**/*.py"],
            "system_prompt": "test",
        }
    )
    assert ed.tools == ["explore", "read_file", "list_dir"]


# ── load_expert_definition — valid inputs ────────────────────────────


def test_valid_all_fields(tmp_path):
    """(a) All fields populated → correct ExpertDefinition."""
    p = _write_yaml(
        tmp_path,
        """\
domain: test-expert
description: A test expert domain.
module_paths:
  - "src/**/*.py"
  - "tests/**/*.py"
system_prompt: You are a test expert for the codebase.
model: test/model-v1
memory:
  max_memory_chars: 4000
  chunk_size: 1000
  max_chunks: 10
  memory_path: /tmp/expert_memory.md
  extras:
    embedding_model: text-embedding-3-small
    similarity_threshold: 0.75
skills:
  - board
  - foo
tools:
  - explore
  - read_file
  - list_dir
  - run_command
extras:
  custom_key: custom_value
  another: 42
""",
    )
    ed = load_expert_definition(p)
    assert ed.domain == "test-expert"
    assert ed.description == "A test expert domain."
    assert ed.module_paths == ["src/**/*.py", "tests/**/*.py"]
    assert ed.system_prompt == "You are a test expert for the codebase."
    assert ed.model == "test/model-v1"
    assert isinstance(ed.memory, ExpertMemoryConfig)
    assert ed.memory.max_memory_chars == 4000
    assert ed.memory.chunk_size == 1000
    assert ed.memory.max_chunks == 10
    assert ed.memory.memory_path == "/tmp/expert_memory.md"
    assert ed.memory.extras == {
        "embedding_model": "text-embedding-3-small",
        "similarity_threshold": 0.75,
    }
    assert ed.skills == ["board", "foo"]
    assert ed.tools == ["explore", "read_file", "list_dir", "run_command"]
    assert ed.extras == {"custom_key": "custom_value", "another": 42}


def test_minimal_valid_yaml(tmp_path):
    """(b) Only required fields → defaults applied for optionals."""
    p = _write_yaml(
        tmp_path,
        """\
domain: minimal-expert
module_paths:
  - "docs/**/*.md"
system_prompt: Do one thing well in the docs domain.
""",
    )
    ed = load_expert_definition(p)
    assert ed.domain == "minimal-expert"
    assert ed.module_paths == ["docs/**/*.md"]
    assert ed.system_prompt == "Do one thing well in the docs domain."
    assert ed.description is None
    assert ed.model == ""
    assert isinstance(ed.memory, ExpertMemoryConfig)
    assert ed.memory.max_memory_chars == 8000
    assert ed.skills == []
    assert ed.tools == ["explore", "read_file", "list_dir"]
    assert ed.extras == {}


def test_memory_defaults_when_key_omitted(tmp_path):
    """(c) Omitting 'memory' key → ExpertMemoryConfig defaults applied."""
    p = _write_yaml(
        tmp_path,
        """\
domain: no-memory-key
module_paths:
  - "src/**/*.py"
system_prompt: test
""",
    )
    ed = load_expert_definition(p)
    assert ed.memory.max_memory_chars == 8000
    assert ed.memory.chunk_size == 2000
    assert ed.memory.max_chunks == 20
    assert ed.memory.memory_path is None
    assert ed.memory.extras == {}


def test_memory_partial_overrides(tmp_path):
    """(d) Partial memory overrides — only specified fields change."""
    p = _write_yaml(
        tmp_path,
        """\
domain: partial-memory
module_paths:
  - "src/**/*.py"
system_prompt: test
memory:
  chunk_size: 4000
""",
    )
    ed = load_expert_definition(p)
    assert ed.memory.chunk_size == 4000
    assert ed.memory.max_memory_chars == 8000  # default
    assert ed.memory.max_chunks == 20  # default
    assert ed.memory.memory_path is None  # default
    assert ed.memory.extras == {}  # default


def test_env_var_substitution_in_model(tmp_path, monkeypatch):
    """(e) ${VAR} in model field is resolved from environment."""
    monkeypatch.setenv("MY_EXPERT_MODEL", "anthropic/claude-4")
    p = _write_yaml(
        tmp_path,
        """\
domain: env-expert
module_paths:
  - "src/**/*.py"
system_prompt: test
model: ${MY_EXPERT_MODEL}
""",
    )
    ed = load_expert_definition(p)
    assert ed.model == "anthropic/claude-4"


def test_non_model_fields_preserve_literal_env_var(tmp_path, monkeypatch):
    """system_prompt containing ${SOMETHING} stays literal."""
    monkeypatch.setenv("SOMETHING", "resolved")
    p = _write_yaml(
        tmp_path,
        """\
domain: literal-expert
module_paths:
  - "src/**/*.py"
system_prompt: "Use ${SOMETHING} as a tag."
model: gpt-4
""",
    )
    ed = load_expert_definition(p)
    assert ed.system_prompt == "Use ${SOMETHING} as a tag."


def test_unresolvable_env_var_resolves_to_empty(tmp_path):
    """Unset env var in model → resolved to ''."""
    if "UNSET_MODEL_VAR" in os.environ:
        del os.environ["UNSET_MODEL_VAR"]
    p = _write_yaml(
        tmp_path,
        """\
domain: bad-env-expert
module_paths:
  - "src/**/*.py"
system_prompt: test
model: ${UNSET_MODEL_VAR}
""",
    )
    ed = load_expert_definition(p)
    assert ed.model == ""


# ── load_expert_definition — error cases ─────────────────────────────


def test_missing_required_domain(tmp_path):
    """(f) Missing 'domain' → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
module_paths:
  - "src/**/*.py"
system_prompt: test
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "domain" in err_str.lower() or "Field required" in err_str


def test_missing_required_module_paths(tmp_path):
    """(g) Missing 'module_paths' → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
domain: no-paths
system_prompt: test
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "module_paths" in err_str.lower() or "Field required" in err_str


def test_missing_required_system_prompt(tmp_path):
    """(h) Missing 'system_prompt' → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
domain: no-prompt
module_paths:
  - "src/**/*.py"
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "system_prompt" in err_str.lower() or "Field required" in err_str


def test_wrong_type_tools(tmp_path):
    """(i) tools: 123 (int) → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
domain: bad-tools
module_paths:
  - "src/**/*.py"
system_prompt: test
tools: 123
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "tools" in err_str.lower() or "list" in err_str.lower()


def test_wrong_type_skills(tmp_path):
    """skills: "not-a-list" → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
domain: bad-skills
module_paths:
  - "src/**/*.py"
system_prompt: test
skills: "not-a-list"
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "skills" in err_str.lower() or "list" in err_str.lower()


def test_unknown_top_level_key(tmp_path):
    """(j) Unknown top-level key → ValidationError (from extra='forbid')."""
    p = _write_yaml(
        tmp_path,
        """\
domain: extra-key
module_paths:
  - "src/**/*.py"
system_prompt: test
bogus_field: should-fail
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "bogus_field" in err_str.lower() or "Extra inputs" in err_str


def test_malformed_yaml_syntax(tmp_path):
    """(k) Invalid YAML (tab-indented) → yaml.YAMLError."""
    p = _write_yaml(
        tmp_path,
        """\
domain: bad-yaml
module_paths:
  - "src/**/*.py"
\tsystem_prompt: tab-indented
""",
    )
    with pytest.raises(yaml.YAMLError):
        load_expert_definition(p)


def test_non_slug_domain(tmp_path):
    """(l) Domain with spaces → ValidationError from @field_validator."""
    p = _write_yaml(
        tmp_path,
        """\
domain: Python Backend
module_paths:
  - "src/**/*.py"
system_prompt: test
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "domain" in err_str.lower() or "slug" in err_str.lower()


def test_non_slug_domain_uppercase(tmp_path):
    """Domain with uppercase letters → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
domain: PythonBackend
module_paths:
  - "src/**/*.py"
system_prompt: test
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "domain" in err_str.lower() or "slug" in err_str.lower()


def test_non_slug_domain_leading_hyphen(tmp_path):
    """Domain starting with hyphen → ValidationError."""
    p = _write_yaml(
        tmp_path,
        """\
domain: -bad-domain
module_paths:
  - "src/**/*.py"
system_prompt: test
""",
    )
    with pytest.raises(Exception) as exc_info:
        load_expert_definition(p)
    err_str = str(exc_info.value)
    assert "domain" in err_str.lower() or "slug" in err_str.lower()


def test_extras_passthrough(tmp_path):
    """(m) Arbitrary keys inside extras survive model_validate round-trip."""
    p = _write_yaml(
        tmp_path,
        """\
domain: extras-test
module_paths:
  - "src/**/*.py"
system_prompt: test
extras:
  foo: bar
  baz: 42
  nested:
    key: value
""",
    )
    ed = load_expert_definition(p)
    assert ed.extras == {"foo": "bar", "baz": 42, "nested": {"key": "value"}}


def test_file_not_found():
    """(p) Non-existent path → FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_expert_definition(Path("/nonexistent/expert.yaml"))


def test_default_tools_value(tmp_path):
    """(q) Default tools is ['explore', 'read_file', 'list_dir'], not []."""
    p = _write_yaml(
        tmp_path,
        """\
domain: default-tools
module_paths:
  - "src/**/*.py"
system_prompt: test
""",
    )
    ed = load_expert_definition(p)
    assert ed.tools == ["explore", "read_file", "list_dir"]
    assert ed.tools != []


# ── independence from agent runtime ──────────────────────────────────


def test_loader_independent_of_agent_runtime():
    """(n) Importing expert_loader does NOT import pydantic_ai,
    robotsix_mill.agents.base, or robotsix_mill.config."""
    code = """
import sys
from pathlib import Path

sys.path.insert(0, "src")

from robotsix_mill.agents.expert_loader import load_expert_definition, ExpertDefinition

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
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "OK" in result.stdout


# ── sample YAML ──────────────────────────────────────────────────────


def test_sample_yaml_parses():
    """(o) expert_definitions/python-backend.yaml parses without error."""
    p = Path("expert_definitions/python-backend.yaml")
    assert p.exists(), "Sample YAML file not found"
    ed = load_expert_definition(p)
    assert ed.domain == "python-backend"
    assert ed.description is not None and "Python backend" in ed.description
    assert "src/**/*.py" in ed.module_paths
    assert isinstance(ed.system_prompt, str) and len(ed.system_prompt) > 0
    assert ed.memory.max_memory_chars == 8000
    assert "board-report" in ed.skills
    assert "run_command" in ed.tools


# ── structural: model_validate direct ─────────────────────────────────


def test_expert_definition_extras_default_empty():
    """extras defaults to {} when not specified."""
    ed = ExpertDefinition.model_validate(
        {
            "domain": "test-domain",
            "module_paths": ["src/**/*.py"],
            "system_prompt": "test",
        }
    )
    assert ed.extras == {}


def test_expert_definition_memory_defaults():
    """memory defaults to ExpertMemoryConfig() when not specified."""
    ed = ExpertDefinition.model_validate(
        {
            "domain": "test-domain",
            "module_paths": ["src/**/*.py"],
            "system_prompt": "test",
        }
    )
    assert isinstance(ed.memory, ExpertMemoryConfig)
    assert ed.memory.max_memory_chars == 8000
    assert ed.memory.chunk_size == 2000
    assert ed.memory.max_chunks == 20
