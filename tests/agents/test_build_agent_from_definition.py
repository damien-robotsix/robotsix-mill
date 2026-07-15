"""Tests for build_agent_from_definition — bridges YAML loader ↔ agent runtime."""

from pathlib import Path
from unittest import mock

import pytest

from robotsix_mill.agents.yaml_loader import AgentDefinition, load_agent_definition


# ── helpers ──────────────────────────────────────────────────────────


def _make_definition(**overrides) -> AgentDefinition:
    """Minimal valid AgentDefinition with *overrides* applied."""
    defaults: dict = dict(
        name="test-agent",
        level=2,
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

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    return captured


# ── happy path ───────────────────────────────────────────────────────


def test_happy_path_passes_all_fields(monkeypatch):
    """All definition fields map to the correct build_agent kwargs."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        name="refine",
        level=3,
        system_prompt="Refine tickets.",
        web_knowledge=True,
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
    assert kwargs["level"] == 3
    assert kwargs["web_knowledge"] is True
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

    definition = load_agent_definition(p)

    captured = _capture_build_agent_kwargs(monkeypatch)
    settings = Settings()

    result = build_agent_from_definition(settings, definition, tools=[])
    assert result is mock.sentinel.AGENT_HANDLE
    assert len(captured) == 1
    kwargs = captured[0]

    assert kwargs["name"] == "refine"
    assert kwargs["system_prompt"] == definition.system_prompt
    # refine runs on capability level 3 (Claude SDK opus).
    assert kwargs["level"] == definition.level == 3
    assert kwargs["web_knowledge"] is True
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


def test_override_replaces_level(monkeypatch):
    """level override replaces definition.level."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(level=1)
    settings = Settings()

    build_agent_from_definition(settings, definition, level=3)

    kwargs = captured[0]
    assert kwargs["level"] == 3


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

    build_agent_from_definition(settings, definition, output_type=int)

    kwargs = captured[0]
    assert kwargs["output_type"] is int


def test_override_multiple_fields(monkeypatch):
    """Multiple overrides are all applied."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        name="original",
        level=1,
        system_prompt="Original.",
        web_knowledge=False,
        retries=1,
    )
    settings = Settings()

    build_agent_from_definition(
        settings,
        definition,
        name="overridden",
        level=3,
        system_prompt="Overridden.",
        retries=5,
    )

    kwargs = captured[0]
    assert kwargs["name"] == "overridden"
    assert kwargs["level"] == 3
    assert kwargs["system_prompt"] == "Overridden."
    assert kwargs["retries"] == 5
    # Non-overridden fields stay from definition.
    assert kwargs["web_knowledge"] is False


# ── end-to-end: real refine.yaml → working agent ──────────────────────


def test_refine_yaml_end_to_end_tool_injection(monkeypatch):
    """End-to-end: load refine.yaml via load_agent_definition, build
    the agent via build_agent_from_definition, and verify that
    web_knowledge=True and report_issue=True cause the
    ask_web_knowledge and report_issue tools to be injected."""
    from pathlib import Path

    from robotsix_mill.agents.yaml_loader import load_agent_definition
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    p = Path("agent_definitions/refine.yaml")
    if not p.exists():
        pytest.skip("agent_definitions/refine.yaml not found")

    definition = load_agent_definition(p)

    # Provide a fake API key so build_agent can construct the model
    # (model construction is local — no network call).
    from robotsix_mill.config import Secrets, _reset_secrets
    import robotsix_mill.config as _cfg

    _reset_secrets()
    _cfg._secrets = Secrets(openrouter_api_key="sk-fake")
    settings = Settings()
    # Use a minimal system prompt to avoid the prompt→tool consistency
    # check. The real refine.yaml prompt references `` `parallel_explore( ``,
    # which triggers a build-time error when tools=[] because other tests
    # may have registered ``parallel_explore`` in the global ToolRegistry.
    # This test validates tool *injection*, not prompt validation.
    # Force the DeepSeek (pydantic-ai) transport so we can inspect the
    # function toolset; refine.yaml is level 3 (Claude SDK) by default,
    # whose handle has no _function_toolset. Tool *injection* (what this
    # test validates) is transport-independent.
    agent = build_agent_from_definition(
        settings,
        definition,
        tools=[],
        level=1,
        system_prompt="Refine tickets using the available tools.",
    )

    # Inspect the agent's toolset to verify tool injection.
    toolset = agent._agent._function_toolset
    tool_names = set(toolset.tools.keys())

    # web_knowledge=True → ask_web_knowledge gateway tool injected.
    assert definition.web_knowledge is True
    assert "ask_web_knowledge" in tool_names, (
        f"ask_web_knowledge tool not injected despite "
        f"web_knowledge=True. Captured tools: {tool_names}"
    )

    # report_issue=True → report_issue tool injected.
    assert definition.report_issue is True
    assert "report_issue" in tool_names, (
        f"report_issue tool not injected despite report_issue=True. "
        f"Captured tools: {tool_names}"
    )

    assert definition.name == "refine"
    assert definition.output_type == "RefineResult"

    # Clean up the agent's HTTP client.
    agent.close()


# ── AGENT.md injection ────────────────────────────────────────────────


def test_inject_agent_md_when_file_exists(tmp_path, monkeypatch):
    """When repo_dir has AGENT.md and inject_agent_md=True, the
    content is injected into the system prompt."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENT.md").write_text("## Test conventions\n\nBe nice.\n")

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        system_prompt="You are a test agent.",
        inject_agent_md=True,
    )
    settings = Settings()

    build_agent_from_definition(
        settings,
        definition,
        repo_dir=repo,
    )

    kwargs = captured[0]
    assert "## Repository Conventions (from AGENT.md)" in kwargs["system_prompt"]
    assert "<repo_conventions>" in kwargs["system_prompt"]
    assert "## Test conventions" in kwargs["system_prompt"]
    assert "</repo_conventions>" in kwargs["system_prompt"]
    # The original prompt is still there.
    assert kwargs["system_prompt"].startswith("You are a test agent.")


def test_inject_agent_md_when_file_missing(tmp_path, monkeypatch):
    """When repo_dir has no AGENT.md, the system prompt is unchanged."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    repo = tmp_path / "repo"
    repo.mkdir()
    # No AGENT.md created.

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        system_prompt="You are a test agent.",
        inject_agent_md=True,
    )
    settings = Settings()

    build_agent_from_definition(
        settings,
        definition,
        repo_dir=repo,
    )

    kwargs = captured[0]
    assert kwargs["system_prompt"] == "You are a test agent."


def test_inject_agent_md_when_disabled(tmp_path, monkeypatch):
    """When inject_agent_md=False, AGENT.md is NOT injected even if it exists."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENT.md").write_text("## Test conventions\n\nBe nice.\n")

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        system_prompt="You are a test agent.",
        inject_agent_md=False,
    )
    settings = Settings()

    build_agent_from_definition(
        settings,
        definition,
        repo_dir=repo,
    )

    kwargs = captured[0]
    assert kwargs["system_prompt"] == "You are a test agent."


def test_inject_agent_md_when_no_repo_dir(tmp_path, monkeypatch):
    """When repo_dir is None, no injection happens."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        system_prompt="You are a test agent.",
        inject_agent_md=True,
    )
    settings = Settings()

    build_agent_from_definition(
        settings,
        definition,
        # No repo_dir passed.
    )

    kwargs = captured[0]
    assert kwargs["system_prompt"] == "You are a test agent."


def test_inject_agent_md_with_override_prompt(tmp_path, monkeypatch):
    """When system_prompt is overridden, AGENT.md is still appended."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENT.md").write_text("## Test conventions\n\nBe nice.\n")

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        system_prompt="Original prompt.",
        inject_agent_md=True,
    )
    settings = Settings()

    build_agent_from_definition(
        settings,
        definition,
        repo_dir=repo,
        system_prompt="Overridden prompt.",
    )

    kwargs = captured[0]
    assert kwargs["system_prompt"].startswith("Overridden prompt.")
    assert "## Repository Conventions (from AGENT.md)" in kwargs["system_prompt"]
    assert "## Test conventions" in kwargs["system_prompt"]


# ── modules field integration ──────────────────────────────────────────


def test_modules_true_passes_to_build_agent(monkeypatch):
    """When modules=True in the definition, modules=True is passed to build_agent."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(modules=True)
    settings = Settings()

    build_agent_from_definition(settings, definition)

    kwargs = captured[0]
    assert kwargs["modules"] is True


def test_modules_false_is_default(monkeypatch):
    """When modules is not set (default), modules=False is passed to build_agent."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition()  # no modules field
    settings = Settings()

    build_agent_from_definition(settings, definition)

    kwargs = captured[0]
    assert kwargs["modules"] is False


def test_modules_explicit_false_passes_to_build_agent(monkeypatch):
    """When modules=False in the definition, modules=False is passed to build_agent."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(modules=False)
    settings = Settings()

    build_agent_from_definition(settings, definition)

    kwargs = captured[0]
    assert kwargs["modules"] is False


def test_modules_field_present_in_all_real_yamls(tmp_path):
    """Every real agent YAML definition either omits modules (getting
    the default False) or sets it explicitly.  No YAML has an unknown
    modules value."""
    from pathlib import Path

    from robotsix_mill.agents.yaml_loader import load_agent_definition

    for yaml_path in Path("agent_definitions").glob("*.yaml"):
        definition = load_agent_definition(yaml_path)
        # modules is always a bool (either default False or explicit)
        assert isinstance(definition.modules, bool), (
            f"{yaml_path.name}: modules must be bool, got "
            f"{type(definition.modules).__name__}"
        )
        # refine.yaml is the only agent that has opted in to modules.
        # All others must still be False.
        if yaml_path.name != "refine.yaml":
            assert definition.modules is False, (
                f"{yaml_path.name}: modules must be False (only refine.yaml "
                f"has opted in so far)"
            )


# ── inject_language_conventions ──────────────────────────────────────


def test_inject_language_conventions_appends_block(monkeypatch, tmp_path):
    """With the flag set and a repo_dir, the resolved language conventions
    are appended to the system prompt under a ## Language conventions head."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.resolve_language_instructions",
        lambda settings, repo_dir: "PEP 758: `except A, B:` is valid 3.14 syntax.",
    )
    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        system_prompt="Review the diff.", inject_language_conventions=True
    )

    build_agent_from_definition(Settings(), definition, repo_dir=tmp_path)
    sp = captured[0]["system_prompt"]
    assert sp.startswith("Review the diff.")
    assert "## Language conventions" in sp
    assert "PEP 758" in sp


def test_inject_language_conventions_disabled_by_default(monkeypatch, tmp_path):
    """Default (flag False) leaves the prompt untouched even with a repo_dir."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.resolve_language_instructions",
        lambda settings, repo_dir: "SHOULD NOT APPEAR",
    )
    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(system_prompt="Refine.")  # flag defaults False

    build_agent_from_definition(Settings(), definition, repo_dir=tmp_path)
    assert captured[0]["system_prompt"] == "Refine."


def test_inject_language_conventions_skipped_without_repo_dir(monkeypatch):
    """No repo_dir → nothing to resolve → prompt untouched."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.resolve_language_instructions",
        lambda settings, repo_dir: "SHOULD NOT APPEAR",
    )
    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        system_prompt="Retrospect.", inject_language_conventions=True
    )

    build_agent_from_definition(Settings(), definition, repo_dir=None)
    assert captured[0]["system_prompt"] == "Retrospect."


def test_inject_language_conventions_empty_block_no_header(monkeypatch, tmp_path):
    """An empty resolved block (repo declares no language) adds no header."""
    from robotsix_mill.agents.base import build_agent_from_definition
    from robotsix_mill.config import Settings

    monkeypatch.setattr(
        "robotsix_mill.config.repo_settings.resolve_language_instructions",
        lambda settings, repo_dir: "   ",
    )
    captured = _capture_build_agent_kwargs(monkeypatch)
    definition = _make_definition(
        system_prompt="Review.", inject_language_conventions=True
    )

    build_agent_from_definition(Settings(), definition, repo_dir=tmp_path)
    assert captured[0]["system_prompt"] == "Review."
    assert "Language conventions" not in captured[0]["system_prompt"]


def test_review_type_agents_declare_language_conventions():
    """Invariant lock: the code-critiquing agents (retrospect, review) must
    keep inject_language_conventions=True so they receive the repo's Python
    conventions (e.g. PEP-758) and don't misjudge valid 3.14 syntax."""
    for name in ("retrospect", "review"):
        d = load_agent_definition(Path("agent_definitions") / f"{name}.yaml")
        assert d.inject_language_conventions is True, name
