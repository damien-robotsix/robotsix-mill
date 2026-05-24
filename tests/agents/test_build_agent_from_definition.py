"""Tests for build_agent_from_definition — bridges YAML loader ↔ agent runtime."""

import os
from pathlib import Path
from unittest import mock

import pytest

from robotsix_mill.agents.yaml_loader import AgentDefinition, load_agent_definition


# ── helpers ──────────────────────────────────────────────────────────

def _make_definition(**overrides) -> AgentDefinition:
    """Minimal valid AgentDefinition with *overrides* applied."""
    defaults: dict = dict(
        name="test-agent",
        model="test/model",
        system_prompt="You are a test agent.",
    )
    defaults.update(overrides)
    return AgentDefinition.model_validate(defaults)


def _capture_build_agent_kwargs(monkeypatch):
    """Monkeypatch build_agent to capture its kwargs dict and return a
    fake AgentHandle. Returns the list that will receive the kwargs."""
    captured: list = []

    def fake_build_agent(settings, **kwargs):
        captured.append(kwargs)
        # Return a sentinel so callers can assert the return value.
        return mock.sentinel.AGENT_HANDLE

    monkeypatch.setattr(
        "robotsix_mill.agents.base.build_agent", fake_build_agent
    )
    return captured


# ── happy path ───────────────────────────────────────────────────────

def test_happy_path_passes_all_fields(monkeypatch):
    """All definition fields map to the correct build_agent kwargs."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        name="refine",
        model="anthropic/claude-opus",
        system_prompt="Refine tickets.",
        web=True,
        report_issue=True,
        retries=3,
        output_type=None,  # str output
    )
    settings = Settings()

    result = build_agent_from_definition(settings, definition, tools=["fake_tool"])
    assert result is mock.sentinel.AGENT_HANDLE
    assert len(captured) == 1
    kwargs = captured[0]

    assert kwargs["name"] == "refine"
    assert kwargs["system_prompt"] == "Refine tickets."
    assert kwargs["model_name"] == "anthropic/claude-opus"
    assert kwargs["web"] is True
    assert kwargs["report_issue"] is True
    assert kwargs["retries"] == 3
    assert kwargs["output_type"] is str
    assert kwargs["tools"] == ["fake_tool"]


def test_real_refine_yaml_builds(monkeypatch):
    """The real agent_definitions/refine.yaml produces kwargs matching
    the refine agent's expected configuration."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    p = Path("agent_definitions/refine.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/refine.yaml not found")

    os.environ.setdefault("MILL_REFINE_MODEL", "test/model")
    try:
        definition = load_agent_definition(p)
    finally:
        if "MILL_REFINE_MODEL" not in os.environ:
            os.environ.pop("MILL_REFINE_MODEL", None)

    captured = _capture_build_agent_kwargs(monkeypatch)
    settings = Settings()

    result = build_agent_from_definition(settings, definition, tools=[])
    assert result is mock.sentinel.AGENT_HANDLE
    assert len(captured) == 1
    kwargs = captured[0]

    assert kwargs["name"] == "refine"
    assert kwargs["system_prompt"] == definition.system_prompt
    assert kwargs["model_name"] == "test/model"
    assert kwargs["web"] is True
    assert kwargs["report_issue"] is True
    assert kwargs["retries"] == 2
    # output_type should be PromptedOutput(RefineResult)
    assert kwargs["output_type"] is not str
    assert kwargs["tools"] == []


# ── output_type resolution ───────────────────────────────────────────

def test_output_type_resolves_from_module(monkeypatch):
    """When output_type + module are set, the class is imported and
    wrapped in PromptedOutput."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings
    from robotsix_mill.agents.refining import RefineResult

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        output_type="RefineResult",
        module="refining",
    )
    settings = Settings()

    build_agent_from_definition(settings, definition)

    kwargs = captured[0]
    # It's PromptedOutput wrapping RefineResult.
    from pydantic_ai import PromptedOutput

    assert isinstance(kwargs["output_type"], PromptedOutput)
    # The wrapped type is stored internally; verify the repr names it.
    assert "RefineResult" in repr(kwargs["output_type"])


def test_output_type_none_defaults_to_str(monkeypatch):
    """When output_type is None, output_type=str is passed."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(output_type=None)
    settings = Settings()

    build_agent_from_definition(settings, definition)

    kwargs = captured[0]
    assert kwargs["output_type"] is str


def test_output_type_empty_string_defaults_to_str(monkeypatch):
    """When output_type is an empty string, output_type=str is passed."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(output_type="")
    settings = Settings()

    build_agent_from_definition(settings, definition)

    kwargs = captured[0]
    assert kwargs["output_type"] is str


def test_module_none_but_output_type_set_raises_valueerror():
    """When module is None but output_type is set → ValueError."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    definition = _make_definition(
        output_type="SomeResult",
        module=None,
    )
    settings = Settings()

    with pytest.raises(ValueError, match="module is None"):
        build_agent_from_definition(settings, definition)


# ── override precedence ──────────────────────────────────────────────

def test_override_replaces_system_prompt(monkeypatch):
    """system_prompt override replaces definition.system_prompt."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(system_prompt="Original prompt.")
    settings = Settings()

    build_agent_from_definition(
        settings, definition, system_prompt="Overridden prompt."
    )

    kwargs = captured[0]
    assert kwargs["system_prompt"] == "Overridden prompt."


def test_override_replaces_model_name(monkeypatch):
    """model_name override replaces definition.model."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(model="definition/model")
    settings = Settings()

    build_agent_from_definition(
        settings, definition, model_name="override/model"
    )

    kwargs = captured[0]
    assert kwargs["model_name"] == "override/model"


def test_override_replaces_output_type(monkeypatch):
    """output_type override replaces the resolved output_type."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        output_type="RefineResult",
        module="refining",
    )
    settings = Settings()

    build_agent_from_definition(
        settings, definition, output_type=int
    )

    kwargs = captured[0]
    assert kwargs["output_type"] is int


def test_override_multiple_fields(monkeypatch):
    """Multiple overrides are all applied."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        name="original",
        model="original/model",
        system_prompt="Original.",
        web=False,
        retries=1,
    )
    settings = Settings()

    build_agent_from_definition(
        settings,
        definition,
        name="overridden",
        model_name="override/model",
        system_prompt="Overridden.",
        retries=5,
    )

    kwargs = captured[0]
    assert kwargs["name"] == "overridden"
    assert kwargs["model_name"] == "override/model"
    assert kwargs["system_prompt"] == "Overridden."
    assert kwargs["retries"] == 5
    # Non-overridden fields stay from definition.
    assert kwargs["web"] is False
