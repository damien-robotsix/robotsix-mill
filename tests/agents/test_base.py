"""Tests for :mod:`robotsix_mill.agents.base` — AgentHandle and agent factory functions."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from robotsix_mill.config import Settings

# ---------------------------------------------------------------------------
# fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> Settings:
    return Settings(
        claude_sdk_vision_enabled=False,
    )


# ---------------------------------------------------------------------------
# _close_async_client
# ---------------------------------------------------------------------------


def test_close_async_client_calls_aclose(monkeypatch):
    """_close_async_client spins a new event loop and calls client.aclose()."""
    from robotsix_mill.agents.base import _close_async_client

    client = MagicMock()
    client.aclose = MagicMock(return_value=asyncio.sleep(0))

    _close_async_client(client)

    client.aclose.assert_called_once()


def test_close_async_client_swallows_exceptions(monkeypatch):
    """_close_async_client does not raise when aclose() fails."""
    from robotsix_mill.agents.base import _close_async_client

    client = MagicMock()
    client.aclose = MagicMock(side_effect=RuntimeError("boom"))

    # Should not raise.
    _close_async_client(client)


# ---------------------------------------------------------------------------
# _aclose_async_client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aclose_async_client_calls_aclose():
    """_aclose_async_client awaits client.aclose()."""
    from robotsix_mill.agents.base import _aclose_async_client

    client = MagicMock()
    client.aclose = MagicMock(return_value=asyncio.sleep(0))

    await _aclose_async_client(client)

    client.aclose.assert_called_once()


@pytest.mark.asyncio
async def test_aclose_async_client_swallows_exceptions():
    """_aclose_async_client does not raise when aclose() fails."""
    from robotsix_mill.agents.base import _aclose_async_client

    client = MagicMock()
    client.aclose = MagicMock(side_effect=RuntimeError("boom"))

    # Should not raise.
    await _aclose_async_client(client)


# ---------------------------------------------------------------------------
# _safe_close
# ---------------------------------------------------------------------------


def test_safe_close_calls_close_on_object_with_close_method():
    """_safe_close calls .close() when the object has a close method."""
    from robotsix_mill.agents.base import _safe_close

    obj = MagicMock()
    _safe_close(obj)

    obj.close.assert_called_once()


def test_safe_close_noops_on_object_without_close():
    """_safe_close does nothing when the object lacks a close method."""
    from robotsix_mill.agents.base import _safe_close

    _safe_close("plain string")  # no close attribute → no-op


def test_safe_close_swallows_exceptions_from_close():
    """_safe_close does not raise when .close() itself raises."""
    from robotsix_mill.agents.base import _safe_close

    obj = MagicMock()
    obj.close.side_effect = RuntimeError("close failed")

    # Should not raise.
    _safe_close(obj)


# ---------------------------------------------------------------------------
# default_tier_config — the llmio tier mapping (replaces _resolve_level)
# ---------------------------------------------------------------------------


def test_default_tier_config_maps_levels():
    """Each capability level maps to its baked (provider, model) via
    llmio's default_tier_config: L1/L2 → DeepSeek-on-OpenRouter, L3 → Claude SDK."""
    from robotsix_llmio.core.factory import default_tier_config
    from robotsix_llmio.core.identifier import parse_model_identifier

    parsed1 = parse_model_identifier(default_tier_config().for_level(1).model)
    assert parsed1.provider == "openrouter"
    assert parsed1.model_name == "deepseek/deepseek-v4-flash"

    parsed2 = parse_model_identifier(default_tier_config().for_level(2).model)
    assert parsed2.provider == "openrouter"
    assert parsed2.model_name == "deepseek/deepseek-v4-pro"

    parsed3 = parse_model_identifier(default_tier_config().for_level(3).model)
    assert parsed3.provider == "claudeSDK"
    assert parsed3.model_name == "opus"


def test_level_uses_claude_only_for_level_3():
    """level_uses_claude is True only for the Claude-SDK transport (L3)."""
    from robotsix_mill.agents.base import level_uses_claude

    assert level_uses_claude(3) is True
    assert level_uses_claude(1) is False
    assert level_uses_claude(2) is False


# ---------------------------------------------------------------------------
# claude_sdk_supports_inline_image
# ---------------------------------------------------------------------------


def test_claude_sdk_supports_inline_image_true():
    """Returns True when claude_sdk_vision_enabled is True."""
    from robotsix_mill.agents.base import claude_sdk_supports_inline_image

    s = Settings(claude_sdk_vision_enabled=True)
    assert claude_sdk_supports_inline_image(s) is True


def test_claude_sdk_supports_inline_image_false():
    """Returns False when claude_sdk_vision_enabled is False."""
    from robotsix_mill.agents.base import claude_sdk_supports_inline_image

    s = Settings(claude_sdk_vision_enabled=False)
    assert claude_sdk_supports_inline_image(s) is False


def test_claude_sdk_supports_inline_image_default_false():
    """Default Settings (vision_enabled not set) returns False."""
    from robotsix_mill.agents.base import claude_sdk_supports_inline_image

    s = Settings()
    # claude_sdk_vision_enabled defaults to False
    assert claude_sdk_supports_inline_image(s) is False


# ---------------------------------------------------------------------------
# AgentHandle
# ---------------------------------------------------------------------------


def test_agent_handle_delegates_attribute_access():
    """AgentHandle delegates attribute access to the wrapped agent."""
    from robotsix_mill.agents.base import AgentHandle

    agent = MagicMock()
    agent.run_sync = MagicMock(return_value="result")
    client = MagicMock()

    handle = AgentHandle(agent, client)
    # Attribute access passes through to the agent.
    assert handle.run_sync() == "result"
    agent.run_sync.assert_called_once()


def test_agent_handle_close_calls_close_async_client(monkeypatch):
    """AgentHandle.close() calls _close_async_client with the http client."""
    from robotsix_mill.agents.base import AgentHandle

    agent = MagicMock()
    client = MagicMock()

    handle = AgentHandle(agent, client)
    handle.close()

    # The _close_async_client is called inside close(); we verify the
    # close method was invoked by checking that client is set to None.
    assert handle._http_client is None


def test_agent_handle_close_is_idempotent():
    """Calling AgentHandle.close() multiple times is safe."""
    from robotsix_mill.agents.base import AgentHandle

    agent = MagicMock()
    client = MagicMock()

    handle = AgentHandle(agent, client)
    handle.close()
    handle.close()  # second close should no-op silently

    assert handle._http_client is None


# ---------------------------------------------------------------------------
# build_agent — DeepSeek path
# ---------------------------------------------------------------------------


def test_build_agent_deepseek_default_path(monkeypatch, settings):
    """build_agent constructs an AgentHandle via _build_deepseek_handle when
    the resolved transport is DeepSeek/OpenRouter (levels 1 & 2)."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    # Capture the kwargs passed to _build_deepseek_handle.
    captured_kwargs: list[dict] = []

    def fake_build_deepseek(settings, **kwargs):
        captured_kwargs.append(kwargs)
        handle = MagicMock()
        handle._agent = MagicMock()
        handle._http_client = MagicMock()
        return handle

    monkeypatch.setattr(bmod, "_build_deepseek_handle", fake_build_deepseek)

    bmod.build_agent(
        settings,
        system_prompt="Test prompt.",
        level=1,
        name="test-agent",
        retries=3,
        output_type=str,
        tools=[],
    )

    assert len(captured_kwargs) == 1
    kw = captured_kwargs[0]
    # level 1 resolves to the flash model via llmio's tier defaults.
    assert kw["effective_model"] == "deepseek/deepseek-v4-flash"
    assert kw["level"] == 1
    assert _cfg._secrets.openrouter_api_key == "sk-test"


def test_build_agent_resolves_level_1_to_flash(monkeypatch, settings):
    """build_agent level 1 resolves to the concrete flash model."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    captured_kwargs: list[dict] = []

    def fake_build_deepseek(settings, **kwargs):
        captured_kwargs.append(kwargs)
        handle = MagicMock()
        handle._agent = MagicMock()
        handle._http_client = MagicMock()
        return handle

    monkeypatch.setattr(bmod, "_build_deepseek_handle", fake_build_deepseek)

    bmod.build_agent(
        settings,
        system_prompt="Test.",
        level=1,
        tools=[],
    )

    assert captured_kwargs[0]["effective_model"] == "deepseek/deepseek-v4-flash"


def test_build_agent_resolves_level_2_to_pro(monkeypatch, settings):
    """build_agent level 2 (the default) resolves to the concrete pro model."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    captured_kwargs: list[dict] = []

    def fake_build_deepseek(settings, **kwargs):
        captured_kwargs.append(kwargs)
        handle = MagicMock()
        handle._agent = MagicMock()
        handle._http_client = MagicMock()
        return handle

    monkeypatch.setattr(bmod, "_build_deepseek_handle", fake_build_deepseek)

    bmod.build_agent(
        settings,
        system_prompt="Test.",
        level=2,
        tools=[],
    )

    assert captured_kwargs[0]["effective_model"] == "deepseek/deepseek-v4-pro"


def test_build_agent_injects_report_issue_tool_by_default(monkeypatch, settings):
    """When report_issue=True (default), the report_issue tool is appended."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    captured_tools: list[list] = []

    def fake_build_deepseek(settings, **kwargs):
        captured_tools.append(kwargs["all_tools"])
        handle = MagicMock()
        handle._agent = MagicMock()
        handle._http_client = MagicMock()
        return handle

    monkeypatch.setattr(bmod, "_build_deepseek_handle", fake_build_deepseek)

    bmod.build_agent(
        settings,
        system_prompt="Test.",
        tools=[MagicMock()],
        report_issue=True,
    )

    # Should have the original tool + the report_issue tool.
    assert len(captured_tools[0]) >= 2


def test_build_agent_report_issue_false_omits_tool(monkeypatch, settings):
    """When report_issue=False, only the explicit tools are passed."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    captured_tools: list[list] = []

    def fake_build_deepseek(settings, **kwargs):
        captured_tools.append(kwargs["all_tools"])
        handle = MagicMock()
        handle._agent = MagicMock()
        handle._http_client = MagicMock()
        return handle

    monkeypatch.setattr(bmod, "_build_deepseek_handle", fake_build_deepseek)

    explicit_tool = MagicMock()
    bmod.build_agent(
        settings,
        system_prompt="Test.",
        tools=[explicit_tool],
        report_issue=False,
        reply_to_thread=False,
        close_thread=False,
        list_threads=False,
        ask_user=False,
    )

    # Only the explicit tool should be present.
    assert captured_tools[0] == [explicit_tool]


def test_build_agent_composes_prompt(monkeypatch, settings):
    """build_agent calls compose_prompt with the right arguments."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    captured_compose: list[dict] = []

    def fake_compose_prompt(
        settings, system_prompt, skills=None, modules=False, workflows=False
    ):
        captured_compose.append(
            dict(
                system_prompt=system_prompt,
                skills=skills,
                modules=modules,
                workflows=workflows,
            )
        )
        return system_prompt

    monkeypatch.setattr(bmod, "compose_prompt", fake_compose_prompt)
    monkeypatch.setattr(
        bmod, "_build_deepseek_handle", lambda settings, **kw: MagicMock()
    )

    bmod.build_agent(
        settings,
        system_prompt="Raw prompt.",
        skills=["board"],
        modules=True,
    )

    assert len(captured_compose) == 1
    assert captured_compose[0]["system_prompt"] == "Raw prompt."
    assert captured_compose[0]["skills"] == ["board"]
    assert captured_compose[0]["modules"] is True
    assert captured_compose[0]["workflows"] is False


def test_build_agent_unregistered_tool_in_prompt_raises(monkeypatch, settings):
    """When the composed prompt references a tool not in the agent's
    tool set, build_agent raises ValueError."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.agents.tool_registry import ToolInfo, ToolRegistry
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    # Snapshot and restore _tools so the fake_tool registration
    # doesn't leak into other tests.
    saved_tools = dict(ToolRegistry._tools)
    try:
        # Register a known tool name so the guard detects it.
        ToolRegistry.register(
            ToolInfo(
                name="fake_tool",
                description="A fake tool.",
                category="fs",
                parameters={},
            )
        )

        # Compose a prompt that calls `fake_tool(` but don't include
        # fake_tool in the tools list.
        prompt_with_call = "Do something with `fake_tool(…)`."

        monkeypatch.setattr(bmod, "compose_prompt", lambda *a, **kw: prompt_with_call)

        with pytest.raises(ValueError, match="fake_tool"):
            bmod.build_agent(
                settings,
                system_prompt=prompt_with_call,
                tools=[],  # empty — fake_tool is not here
                report_issue=False,
                web_knowledge=False,
            )
    finally:
        ToolRegistry._tools.clear()
        ToolRegistry._tools.update(saved_tools)


def test_build_agent_missing_api_key_raises(monkeypatch, settings):
    """When OPENROUTER_API_KEY is not set, build_agent raises RuntimeError
    on the DeepSeek path."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import _reset_secrets

    _reset_secrets()
    # No key set.

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        bmod.build_agent(
            settings,
            system_prompt="Test.",
            tools=[],
        )


# ---------------------------------------------------------------------------
# build_agent — Claude SDK path (mocked)
# ---------------------------------------------------------------------------


def test_build_agent_claude_sdk_path(monkeypatch):
    """When the resolved transport is the Claude SDK (level 3), build_agent
    delegates to robotsix-llmio's ClaudeSDKProvider via get_provider_for_level."""
    from robotsix_mill.agents import base as bmod
    from robotsix_llmio.claude_sdk.provider import (
        ClaudeSDKProvider as RealClaudeSDKProvider,
    )

    s = Settings()

    # Mock compose_prompt to avoid yaml/path deps.
    monkeypatch.setattr(bmod, "compose_prompt", lambda *a, **kw: "test prompt")

    # Create a mock provider whose build_agent returns a fake handle.
    fake_claude_handle = MagicMock()
    fake_provider = MagicMock(spec=RealClaudeSDKProvider)
    fake_provider.build_agent.return_value = fake_claude_handle

    # Mock get_provider_for_level so the Claude path receives our fake.
    monkeypatch.setattr(
        "robotsix_llmio.core.factory.get_provider_for_level",
        lambda level, tier_config=None, **kwargs: fake_provider,
    )

    # bound_claude_handle is a pass-through in the test.
    monkeypatch.setattr(
        "robotsix_mill.agents.claude_concurrency.bound_claude_handle",
        lambda handle, max_concurrency: handle,
    )

    result = bmod.build_agent(
        s,
        system_prompt="Test prompt.",
        level=3,  # level 3 resolves to the Claude SDK transport
        name="claude-agent",
        tools=[],
    )

    # bound_claude_handle is a pass-through, so result is the raw handle.
    assert result is fake_claude_handle
    # build_agent was called on the provider with level=3.
    fake_provider.build_agent.assert_called_once()
    assert fake_provider.build_agent.call_args.kwargs["level"] == 3
    assert fake_provider.build_agent.call_args.kwargs["system_prompt"] == "test prompt"


# ---------------------------------------------------------------------------
# _build_deepseek_handle
# ---------------------------------------------------------------------------


def test_build_deepseek_handle_constructs_agent(monkeypatch, settings):
    """_build_deepseek_handle constructs a pydantic-ai Agent with the
    correct parameters and returns an AgentHandle. The model + http client
    come from llmio via new_deepseek_model."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    # Mock pydantic_ai.Agent (local import inside _build_deepseek_handle).
    fake_agent = MagicMock()
    fake_agent_cls = MagicMock(return_value=fake_agent)
    monkeypatch.setattr("pydantic_ai.Agent", fake_agent_cls, raising=False)

    # Mock the llmio model-construction seam → (model, http_client).
    fake_model = MagicMock()
    fake_client = MagicMock()
    captured_call: dict = {}

    def fake_new_deepseek(model_name, level):
        captured_call["model_name"] = model_name
        captured_call["level"] = level
        return fake_model, fake_client

    monkeypatch.setattr(bmod, "new_deepseek_model", fake_new_deepseek)

    fake_tool = MagicMock()
    fake_tool.__name__ = "fake_tool"

    handle = bmod._build_deepseek_handle(
        settings,
        effective_model="deepseek/deepseek-v4-flash",
        level=1,
        composed_system="System prompt.",
        all_tools=[fake_tool],
        output_type=str,
        name="test-agent",
        retries=3,
    )

    assert isinstance(handle, bmod.AgentHandle)
    assert handle._agent is fake_agent
    assert handle._http_client is fake_client
    # The model was built from the resolved model name + level.
    assert captured_call == {"model_name": "deepseek/deepseek-v4-flash", "level": 1}

    # Agent was constructed with the right kwargs.
    fake_agent_cls.assert_called_once()
    agent_kwargs = fake_agent_cls.call_args.kwargs
    assert agent_kwargs["model"] is fake_model
    assert agent_kwargs["system_prompt"] == "System prompt."
    assert agent_kwargs["output_type"] is str
    assert agent_kwargs["tools"] == [fake_tool]
    assert agent_kwargs["retries"] == 3
    assert agent_kwargs["name"] == "test-agent"


def test_build_deepseek_handle_with_max_tokens(monkeypatch, settings):
    """When max_tokens is provided, ModelSettings is included in Agent kwargs."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    fake_agent_cls = MagicMock()
    monkeypatch.setattr("pydantic_ai.Agent", fake_agent_cls, raising=False)
    monkeypatch.setattr(
        bmod, "new_deepseek_model", lambda model_name, level: (MagicMock(), MagicMock())
    )

    bmod._build_deepseek_handle(
        settings,
        effective_model="deepseek/deepseek-v4-flash",
        level=1,
        composed_system="System prompt.",
        all_tools=[],
        output_type=str,
        name=None,
        retries=1,
        max_tokens=4096,
    )

    agent_kwargs = fake_agent_cls.call_args.kwargs
    assert "model_settings" in agent_kwargs
    # model_settings is a ModelSettings TypedDict; isinstance doesn't work.
    assert agent_kwargs["model_settings"]["max_tokens"] == 4096


def test_build_deepseek_handle_requires_api_key(monkeypatch, settings):
    """_build_deepseek_handle raises RuntimeError when no API key is set —
    new_deepseek_model guards on the key."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import _reset_secrets

    _reset_secrets()

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        bmod._build_deepseek_handle(
            settings,
            effective_model="deepseek/deepseek-v4-flash",
            level=1,
            composed_system="System prompt.",
            all_tools=[],
            output_type=str,
            name=None,
            retries=1,
        )


# ---------------------------------------------------------------------------
# _render_module_map
# ---------------------------------------------------------------------------


def test_render_module_map_few_modules_lists_all():
    """With ≤20 modules, every module gets a ### heading, description,
    paths, and dependency hints."""
    from robotsix_mill.agents.base import _render_module_map

    modules = [
        {
            "id": "config",
            "description": "Configuration layer.",
            "paths": ["src/robotsix_mill/config/*.py"],
            "dependencies": [],
        },
        {
            "id": "agents",
            "description": "Agent infrastructure.",
            "paths": ["src/robotsix_mill/agents/*.py", "tests/agents/test_base.py"],
            "dependencies": ["config"],
        },
    ]

    result = _render_module_map(modules)

    # Both modules should appear as ### sub-headings.
    assert "### config" in result
    assert "### agents" in result
    # Descriptions should appear.
    assert "Configuration layer." in result
    assert "Agent infrastructure." in result
    # Paths should appear as `-` bullet items.
    assert "- `src/robotsix_mill/config/*.py`" in result
    assert "- `src/robotsix_mill/agents/*.py`" in result
    assert "- `tests/agents/test_base.py`" in result
    # Dependency hint for the module that has dependencies.
    assert "Depends on: config" in result
    # The module with empty dependencies should NOT have a "Depends on:" line.
    assert "Depends on:" not in result.split("### config")[1].split("###")[0]


def test_render_module_map_many_modules_only_top_level():
    """With >20 modules, only top-level (no-dependency) modules are
    rendered, with a pointer to docs/modules.yaml."""
    from robotsix_mill.agents.base import _render_module_map

    # Build 25 modules: 3 top-level (no dependencies), 22 with dependencies.
    modules: list[dict] = []
    for i in range(3):
        modules.append(
            {
                "id": f"top-level-{i}",
                "description": f"Top-level module {i}.",
                "paths": [f"src/top_{i}.py"],
                "dependencies": [],
            }
        )
    for i in range(22):
        modules.append(
            {
                "id": f"sub-module-{i}",
                "description": f"Sub module {i}.",
                "paths": [f"src/sub_{i}.py"],
                "dependencies": ["config"],
            }
        )

    result = _render_module_map(modules)

    # Top-level modules appear.
    for i in range(3):
        assert f"### top-level-{i}" in result
        assert f"Top-level module {i}." in result
    # Sub-modules must NOT appear (they have dependencies).
    for i in range(22):
        assert f"### sub-module-{i}" not in result
    # Pointer to docs/modules.yaml must appear.
    assert "See `docs/modules.yaml` for additional sub-divisions" in result


def test_render_module_map_top_level_without_dependencies_key():
    """Modules missing the ``dependencies`` key are treated as top-level
    (equivalent to an empty list) and appear in the >20 output."""
    from robotsix_mill.agents.base import _render_module_map

    # 21 modules total: 1 without a dependencies key, 20 with dependencies.
    modules: list[dict] = [
        {
            "id": "orphan",
            "description": "No deps key at all.",
            "paths": ["src/orphan.py"],
        }
    ]
    for i in range(20):
        modules.append(
            {
                "id": f"dep-{i}",
                "description": f"Dep module {i}.",
                "paths": [f"src/dep_{i}.py"],
                "dependencies": ["config"],
            }
        )

    result = _render_module_map(modules)

    assert "### orphan" in result
    assert "No deps key at all." in result
    for i in range(20):
        assert f"### dep-{i}" not in result


def test_render_module_map_truncation(monkeypatch):
    """When the rendered output exceeds MODULE_MAP_MAX_CHARS, it is
    truncated on a line boundary and a pointer is appended."""
    import robotsix_mill.agents.base as bmod

    # Force a very small budget to trigger truncation.
    monkeypatch.setattr(bmod, "MODULE_MAP_MAX_CHARS", 200)

    # Use ≤20 modules so every module is rendered (the else branch),
    # but with enough content to exceed the 200-char budget.
    modules: list[dict] = []
    for i in range(10):
        modules.append(
            {
                "id": f"mod-{i}",
                "description": "A" * 80,  # long description to blow the budget
                "paths": [f"src/mod_{i}.py"],
                "dependencies": [],
            }
        )

    result = bmod._render_module_map(modules)

    pointer = "…(module map truncated — see docs/modules.yaml for the full taxonomy)"
    assert pointer in result
    assert result.endswith(pointer)
    # The rendered text must not exceed MODULE_MAP_MAX_CHARS.
    assert len(result) <= 200
    # The prefix before the pointer should not end mid-line — the last
    # character before the pointer should be a newline (since rsplit on
    # \n drops the trailing partial line).  Exception: if the budget was
    # so small that only the header survived, the prefix is just the
    # header line with no trailing newline.
    prefix = result[: -len(pointer)]
    assert prefix.endswith("\n") or prefix == "## Module Map"


# ---------------------------------------------------------------------------
# compose_prompt
# ---------------------------------------------------------------------------


def test_compose_prompt_base_case():
    """With no skills and modules=False, the prompt is returned unchanged."""
    from robotsix_mill.agents.base import compose_prompt

    s = Settings()
    result = compose_prompt(s, "Hello, world.")
    assert result == "Hello, world."


def test_compose_prompt_single_skill_strips_frontmatter(tmp_path):
    """A skill SKILL.md is loaded and its YAML frontmatter is stripped."""
    from robotsix_mill.agents.base import compose_prompt

    skill_dir = tmp_path / "skills" / "board"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ntitle: Board\n---\n\nBoard skill body.\n",
        encoding="utf-8",
    )

    s = Settings(skills_dir=tmp_path / "skills")
    result = compose_prompt(s, "Hello.", skills=["board"])

    assert "## Skills" in result
    assert "Board skill body." in result
    # Frontmatter must be stripped.
    assert "---" not in result
    assert "title:" not in result


def test_compose_prompt_multiple_skills_concatenated(tmp_path):
    """Multiple skills are concatenated under a single ## Skills heading."""
    from robotsix_mill.agents.base import compose_prompt

    for name in ("board", "explore"):
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\ntitle: {name}\n---\n\n{name} skill body.\n",
            encoding="utf-8",
        )

    s = Settings(skills_dir=tmp_path / "skills")
    result = compose_prompt(s, "Hello.", skills=["board", "explore"])

    assert "## Skills" in result
    assert "board skill body." in result
    assert "explore skill body." in result
    # Heading should appear exactly once.
    assert result.count("## Skills") == 1


def test_compose_prompt_missing_skill_logs_warning(tmp_path, caplog):
    """A missing skill file logs a warning but does not crash; prompt unchanged."""
    import logging

    from robotsix_mill.agents.base import compose_prompt

    s = Settings(skills_dir=tmp_path / "skills")
    with caplog.at_level(logging.WARNING, logger="robotsix_mill.agents.base"):
        result = compose_prompt(s, "Hello.", skills=["nonexistent"])

    assert "Skill file not found" in caplog.text
    assert result == "Hello."


def test_compose_prompt_modules_true_appends_map(tmp_path, monkeypatch):
    """modules=True loads docs/modules.yaml and appends a rendered module map."""
    import pathlib

    from robotsix_mill.agents.base import compose_prompt

    # Write a minimal taxonomy to a temp file so we don't depend on the
    # real docs/modules.yaml content.
    modules_yaml = tmp_path / "modules.yaml"
    modules_yaml.write_text(
        """modules:
- id: test-mod
  description: A test module.
  paths:
    - src/test.py
  dependencies: []
""",
        encoding="utf-8",
    )

    original_open = pathlib.Path.open

    def fake_open(self, *args, **kwargs):
        if str(self) == "docs/modules.yaml":
            return original_open(modules_yaml, *args, **kwargs)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", fake_open)

    s = Settings()
    result = compose_prompt(s, "Hello.", modules=True)

    assert "## Module Map" in result
    assert result.startswith("Hello.")
    assert "### test-mod" in result
    assert "A test module." in result
    assert "- `src/test.py`" in result


def test_compose_prompt_missing_modules_yaml_no_crash(monkeypatch):
    """When docs/modules.yaml is missing, the prompt is returned without the
    module block (and no crash)."""
    import pathlib

    from robotsix_mill.agents.base import compose_prompt

    original_open = pathlib.Path.open

    def fake_open(self, *args, **kwargs):
        if str(self) == "docs/modules.yaml":
            raise FileNotFoundError("nope")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", fake_open)

    s = Settings()
    result = compose_prompt(s, "Hello.", modules=True)

    assert "## Module Map" not in result
    assert result == "Hello."


def test_compose_prompt_unparseable_modules_yaml_no_crash(tmp_path, monkeypatch):
    """When docs/modules.yaml is unparseable, the prompt is returned without
    the module block (and no crash)."""
    import io
    import pathlib

    from robotsix_mill.agents.base import compose_prompt

    # Redirect Path("docs/modules.yaml").open() to a StringIO containing
    # bad YAML so that yaml.safe_load naturally raises YAMLError — without
    # globally patching yaml.safe_load (which would break Settings()).
    original_open = pathlib.Path.open

    def fake_open(self, *args, **kwargs):
        if str(self) == "docs/modules.yaml":
            return io.StringIO(": bad: ::: yaml [[[")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "open", fake_open)

    s = Settings()
    result = compose_prompt(s, "Hello.", modules=True)

    assert "## Module Map" not in result
    assert result == "Hello."


# ---------------------------------------------------------------------------
# build_openrouter_model
# ---------------------------------------------------------------------------


def test_build_openrouter_model_resolves_level_to_model(monkeypatch, settings):
    """build_openrouter_model resolves the level to a concrete model and
    delegates to new_deepseek_model, returning its (model, client)."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    fake_model = MagicMock()
    fake_client = MagicMock()
    captured: dict = {}

    def fake_new_deepseek(model_name, level):
        captured["model_name"] = model_name
        captured["level"] = level
        return fake_model, fake_client

    monkeypatch.setattr(bmod, "new_deepseek_model", fake_new_deepseek)

    model, client = bmod.build_openrouter_model(1)

    assert model is fake_model
    assert client is fake_client
    # Level 1 resolves to the flash model via llmio's tier defaults.
    assert captured == {"model_name": "deepseek/deepseek-v4-flash", "level": 1}


def test_build_openrouter_model_online_appends_suffix(monkeypatch, settings):
    """When online=True, the resolved model carries the ``:online`` suffix
    that bills the OpenRouter web-search surcharge."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    captured: dict = {}

    def fake_new_deepseek(model_name, level):
        captured["model_name"] = model_name
        return MagicMock(), MagicMock()

    monkeypatch.setattr(bmod, "new_deepseek_model", fake_new_deepseek)

    bmod.build_openrouter_model(1, online=True)
    assert captured["model_name"] == "deepseek/deepseek-v4-flash:online"
