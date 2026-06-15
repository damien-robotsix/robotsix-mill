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
        llm_backend="openrouter",
        claude_sdk_agents=[],
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
# timeout_http_client
# ---------------------------------------------------------------------------


def test_timeout_http_client_returns_async_client_with_timeout(monkeypatch):
    """timeout_http_client returns an httpx.AsyncClient with the settings timeout."""
    from robotsix_mill.agents.base import timeout_http_client

    s = Settings(model_request_timeout=45.0)

    client = timeout_http_client(s)
    try:
        import httpx

        assert isinstance(client, httpx.AsyncClient)
        assert client.timeout.read == 45.0
        assert client.timeout.connect == 15.0
    finally:
        # Clean up to avoid resource warnings.
        import asyncio as _asyncio

        try:
            loop = _asyncio.new_event_loop()
            loop.run_until_complete(client.aclose())
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _model_name
# ---------------------------------------------------------------------------


def test_model_name_returns_settings_model(settings):
    """_model_name returns the primary Settings.model value."""
    from robotsix_mill.agents.base import _model_name

    settings = Settings(model="deepseek/deepseek-v4-pro")
    assert _model_name(settings) == "deepseek/deepseek-v4-pro"


# ---------------------------------------------------------------------------
# _use_claude_sdk
# ---------------------------------------------------------------------------


def test_use_claude_sdk_global_backend():
    """When llm_backend is 'claude_sdk', _use_claude_sdk returns True."""
    from robotsix_mill.agents.base import _use_claude_sdk

    s = Settings(llm_backend="claude_sdk", claude_sdk_agents=[])
    assert _use_claude_sdk(s, "test-agent") is True


def test_use_claude_sdk_per_agent_opt_in():
    """When the agent name is in claude_sdk_agents, return True
    even if the global backend is openrouter."""
    from robotsix_mill.agents.base import _use_claude_sdk

    s = Settings(
        llm_backend="openrouter", claude_sdk_agents=["my-agent", "other-agent"]
    )
    assert _use_claude_sdk(s, "my-agent") is True
    assert _use_claude_sdk(s, "other-agent") is True
    assert _use_claude_sdk(s, "unlisted-agent") is False


def test_use_claude_sdk_neither_global_nor_listed():
    """When llm_backend is not claude_sdk and agent is not listed → False."""
    from robotsix_mill.agents.base import _use_claude_sdk

    s = Settings(llm_backend="openrouter", claude_sdk_agents=[])
    assert _use_claude_sdk(s, "any-agent") is False


def test_use_claude_sdk_name_is_none():
    """When name is None, the per-agent list check is skipped."""
    from robotsix_mill.agents.base import _use_claude_sdk

    s = Settings(llm_backend="openrouter", claude_sdk_agents=["irrelevant"])
    assert _use_claude_sdk(s, None) is False


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
# MODEL_TIER_ALIASES
# ---------------------------------------------------------------------------


def test_model_tier_aliases_cheap():
    """The 'cheap' alias maps to the flash model."""
    from robotsix_mill.agents.base import _MODEL_TIER_ALIASES

    assert _MODEL_TIER_ALIASES["cheap"] == "deepseek/deepseek-v4-flash"


def test_model_tier_aliases_default():
    """The 'default' alias maps to the pro model."""
    from robotsix_mill.agents.base import _MODEL_TIER_ALIASES

    assert _MODEL_TIER_ALIASES["default"] == "deepseek/deepseek-v4-pro"


def test_model_tier_aliases_normal():
    """The 'normal' alias maps to the pro model (same as default)."""
    from robotsix_mill.agents.base import _MODEL_TIER_ALIASES

    assert _MODEL_TIER_ALIASES["normal"] == "deepseek/deepseek-v4-pro"


# ---------------------------------------------------------------------------
# build_agent — DeepSeek path
# ---------------------------------------------------------------------------


def test_build_agent_deepseek_default_path(monkeypatch, settings):
    """build_agent constructs an AgentHandle via _build_deepseek_handle when
    the backend is openrouter and the agent is not claude_sdk-listed."""
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
        model_name="test-model/v1",
        name="test-agent",
        retries=3,
        output_type=str,
        tools=[],
    )

    assert len(captured_kwargs) == 1
    kw = captured_kwargs[0]
    assert kw["effective_model"] == "test-model/v1"
    assert _cfg._secrets.openrouter_api_key == "sk-test"


def test_build_agent_resolves_tier_alias(monkeypatch, settings):
    """build_agent resolves 'cheap' alias to the concrete flash model."""
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
        model_name="cheap",
        tools=[],
    )

    assert captured_kwargs[0]["effective_model"] == "deepseek/deepseek-v4-flash"


def test_build_agent_resolves_default_alias(monkeypatch, settings):
    """build_agent resolves 'default' alias to the concrete pro model."""
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
        model_name="default",
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

    def fake_compose_prompt(settings, system_prompt, skills=None, modules=False):
        captured_compose.append(
            dict(
                system_prompt=system_prompt,
                skills=skills,
                modules=modules,
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
    """When the agent is routed to the Claude SDK, build_agent delegates to
    robotsix-llmio's ClaudeSDKProvider."""
    from robotsix_mill.agents import base as bmod

    s = Settings(llm_backend="claude_sdk", claude_sdk_agents=[])

    # Make _use_claude_sdk return True.
    monkeypatch.setattr(bmod, "_use_claude_sdk", lambda *a, **kw: True)

    # Mock compose_prompt to avoid yaml/path deps.
    monkeypatch.setattr(bmod, "compose_prompt", lambda *a, **kw: "test prompt")

    # Mock the Claude SDK imports. build_agent uses local imports:
    #   from robotsix_llmio.claude_sdk.provider import ClaudeSDKProvider
    #   from .claude_concurrency import bound_claude_handle
    fake_claude_handle = MagicMock()
    fake_provider = MagicMock()
    fake_provider.build_agent.return_value = fake_claude_handle
    fake_claude_provider_cls = MagicMock(return_value=fake_provider)

    # Patch the source modules so the local imports inside build_agent resolve.
    monkeypatch.setattr(
        "robotsix_llmio.claude_sdk.provider.ClaudeSDKProvider",
        fake_claude_provider_cls,
        raising=False,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.claude_concurrency.bound_claude_handle",
        lambda handle, max_concurrency: handle,
    )

    result = bmod.build_agent(
        s,
        system_prompt="Test prompt.",
        model_name="anthropic/claude-haiku",
        name="claude-agent",
        tools=[],
    )

    # bound_claude_handle is a pass-through, so result is the raw handle.
    assert result is fake_claude_handle
    # The provider was constructed.
    fake_claude_provider_cls.assert_called_once()
    # build_agent was called on the provider.
    fake_provider.build_agent.assert_called_once()


def test_build_agent_claude_sdk_with_fallback(monkeypatch):
    """When claude_fallback_to_deepseek is True and an OpenRouter key exists,
    build_agent wraps the Claude handle in a FallbackAgentHandle."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    s = Settings(
        llm_backend="claude_sdk",
        claude_sdk_agents=[],
        claude_fallback_to_deepseek=True,
    )

    monkeypatch.setattr(bmod, "_use_claude_sdk", lambda *a, **kw: True)
    monkeypatch.setattr(bmod, "compose_prompt", lambda *a, **kw: "test prompt")

    fake_claude_handle = MagicMock()
    fake_provider = MagicMock()
    fake_provider.build_agent.return_value = fake_claude_handle

    monkeypatch.setattr(
        "robotsix_llmio.claude_sdk.provider.ClaudeSDKProvider",
        MagicMock(return_value=fake_provider),
        raising=False,
    )
    monkeypatch.setattr(
        "robotsix_mill.agents.claude_concurrency.bound_claude_handle",
        lambda handle, max_concurrency: handle,
    )

    result = bmod.build_agent(
        s,
        system_prompt="Test prompt.",
        model_name="anthropic/claude-haiku",
        name="claude-agent",
        tools=[],
    )

    # Should be a FallbackAgentHandle, not the raw Claude handle.
    from robotsix_mill.agents.fallback import FallbackAgentHandle

    assert isinstance(result, FallbackAgentHandle)


# ---------------------------------------------------------------------------
# _build_deepseek_handle
# ---------------------------------------------------------------------------


def test_build_deepseek_handle_constructs_agent(monkeypatch, settings):
    """_build_deepseek_handle constructs a pydantic-ai Agent with the
    correct parameters and returns an AgentHandle."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    # Mock pydantic_ai.Agent (local import inside _build_deepseek_handle).
    fake_agent = MagicMock()
    fake_agent_cls = MagicMock(return_value=fake_agent)
    monkeypatch.setattr("pydantic_ai.Agent", fake_agent_cls, raising=False)

    # Mock CostInstrumentedOpenRouterModel (local import from .openrouter_cost).
    fake_model = MagicMock()
    fake_model_cls = MagicMock(return_value=fake_model)
    monkeypatch.setattr(
        "robotsix_mill.agents.openrouter_cost.CostInstrumentedOpenRouterModel",
        fake_model_cls,
    )

    # Mock OpenRouterProvider (local import inside _build_deepseek_handle).
    fake_provider_cls = MagicMock()
    monkeypatch.setattr(
        "pydantic_ai.providers.openrouter.OpenRouterProvider",
        fake_provider_cls,
    )

    # Mock timeout_http_client (module-level function in base.py).
    fake_client = MagicMock()
    monkeypatch.setattr(bmod, "timeout_http_client", lambda s: fake_client)

    fake_tool = MagicMock()
    fake_tool.__name__ = "fake_tool"

    handle = bmod._build_deepseek_handle(
        settings,
        effective_model="deepseek/deepseek-v4-flash",
        composed_system="System prompt.",
        all_tools=[fake_tool],
        output_type=str,
        name="test-agent",
        retries=3,
    )

    assert isinstance(handle, bmod.AgentHandle)
    assert handle._agent is fake_agent
    assert handle._http_client is fake_client

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
        "robotsix_mill.agents.openrouter_cost.CostInstrumentedOpenRouterModel",
        MagicMock(),
    )
    monkeypatch.setattr(
        "pydantic_ai.providers.openrouter.OpenRouterProvider",
        MagicMock(),
    )
    monkeypatch.setattr(bmod, "timeout_http_client", lambda s: MagicMock())

    bmod._build_deepseek_handle(
        settings,
        effective_model="deepseek/deepseek-v4-flash",
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
    """_build_deepseek_handle raises RuntimeError when no API key is set."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import _reset_secrets

    _reset_secrets()

    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        bmod._build_deepseek_handle(
            settings,
            effective_model="deepseek/deepseek-v4-flash",
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
# build_openrouter_model
# ---------------------------------------------------------------------------


def test_build_openrouter_model_constructs_cost_instrumented_model(
    monkeypatch, settings
):
    """build_openrouter_model constructs a CostInstrumentedOpenRouterModel
    with an OpenRouter provider and returns the model + client."""
    from robotsix_mill.agents import base as bmod
    from robotsix_mill.config import Secrets, _reset_secrets

    _reset_secrets()
    import robotsix_mill.config as _cfg

    _cfg._secrets = Secrets(openrouter_api_key="sk-test")

    fake_client = MagicMock()
    monkeypatch.setattr(bmod, "timeout_http_client", lambda s: fake_client)

    fake_model = MagicMock()
    fake_model_cls = MagicMock(return_value=fake_model)
    monkeypatch.setattr(
        "robotsix_mill.agents.openrouter_cost.CostInstrumentedOpenRouterModel",
        fake_model_cls,
    )

    fake_provider = MagicMock()
    fake_provider_cls = MagicMock(return_value=fake_provider)
    monkeypatch.setattr(
        "pydantic_ai.providers.openrouter.OpenRouterProvider",
        fake_provider_cls,
    )

    model, client = bmod.build_openrouter_model(settings, "test-model")

    assert model is fake_model
    assert client is fake_client

    # Model constructed with model name + provider.
    fake_model_cls.assert_called_once_with(
        "test-model",
        provider=fake_provider,
    )

    # Provider constructed with the API key + the timeout client.
    fake_provider_cls.assert_called_once_with(
        api_key="sk-test",
        http_client=fake_client,
    )
