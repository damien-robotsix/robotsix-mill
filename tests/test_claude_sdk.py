"""Claude Agent SDK transport — prompt rendering, usage mapping, guards,
transient, and tool-loop bridge.  Offline only: the live single-turn and
tool round-trip tests are exercised separately (need the ``claude`` CLI
+ login)."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import Tool as PydanticTool

from robotsix_llmio.claude_sdk.model import (
    ClaudeSDKModel,
    _map_usage,
    render_prompt,
)
from robotsix_llmio.claude_sdk.provider import (
    ClaudeSDKProvider,
    _SdkToolAgentHandle,
    _SdkToolResult,
    _convert_tools,
)
from robotsix_llmio.claude_sdk.transient import is_claude_sdk_transient
from robotsix_llmio.core.provider import Tier
from robotsix_llmio.core.agent import AgentHandle


# --- prompt rendering ------------------------------------------------------


def test_single_user_turn_sent_verbatim():
    msgs = [ModelRequest(parts=[UserPromptPart(content="hello there")])]
    assert render_prompt(msgs) == "hello there"


def test_multi_turn_rendered_as_transcript():
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="first")]),
        ModelResponse(parts=[TextPart(content="bad json")]),
        ModelRequest(parts=[RetryPromptPart(content="invalid, retry", tool_name=None)]),
    ]
    out = render_prompt(msgs)
    assert "User: first" in out
    assert "Assistant: bad json" in out
    assert "User:" in out.split("Assistant: bad json")[1]  # retry rendered last


def test_tool_return_part_rendered_as_user_text():
    msgs = [
        ModelRequest(
            parts=[ToolReturnPart(tool_name="lookup", content="42", tool_call_id="c1")]
        )
    ]
    assert "Tool result (lookup): 42" in render_prompt(msgs)


# --- system prompt assembly ------------------------------------------------


def _params(output_mode="text"):
    return ModelRequestParameters(output_mode=output_mode)


def test_system_text_combines_instructions_and_system_parts():
    m = ClaudeSDKModel("opus")
    msgs = [
        ModelRequest(
            parts=[
                SystemPromptPart(content="be terse"),
                UserPromptPart(content="hi"),
            ],
            instructions="answer in french",
        )
    ]
    sys = m._system_text(msgs, _params())
    assert "be terse" in sys and "answer in french" in sys


# --- unsupported-mode guards -----------------------------------------------


def test_rejects_tool_based_output_mode():
    m = ClaudeSDKModel("opus")
    with pytest.raises(UserError, match="PromptedOutput"):
        m._reject_unsupported(_params(output_mode="tool"))


def test_allows_prompted_and_text_modes():
    m = ClaudeSDKModel("opus")
    m._reject_unsupported(_params(output_mode="text"))
    m._reject_unsupported(_params(output_mode="prompted"))  # no raise


def test_rejects_function_tools():
    m = ClaudeSDKModel("opus")

    class _Tool:
        pass

    with pytest.raises(UserError, match="tool calling"):
        m._reject_unsupported(
            ModelRequestParameters(output_mode="text", function_tools=[_Tool()])
        )


# --- usage mapping ---------------------------------------------------------


def test_map_usage_from_result():
    class _R:
        usage = {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 3,
            "cache_creation_input_tokens": 7,
        }

    u = _map_usage(_R())
    assert (u.input_tokens, u.output_tokens) == (10, 5)
    assert (u.cache_read_tokens, u.cache_write_tokens) == (3, 7)


def test_map_usage_handles_none_and_partial():
    assert _map_usage(None).input_tokens == 0

    class _R:
        usage = {"input_tokens": 4}

    assert _map_usage(_R()).output_tokens == 0


# --- model identity --------------------------------------------------------


def test_model_name_defaults_to_sdk_model_and_system_is_anthropic():
    m = ClaudeSDKModel("haiku")
    assert m.model_name == "haiku"
    assert m.system == "anthropic"
    assert m.provider is None


# --- transient -------------------------------------------------------------


def test_sdk_subprocess_errors_are_transient():
    class CLIConnectionError(Exception):
        pass

    assert is_claude_sdk_transient(CLIConnectionError("lost cli")) is True


def test_plain_value_error_not_transient():
    assert is_claude_sdk_transient(ValueError("nope")) is False


# ---------------------------------------------------------------------------
# Helpers for tool-loop bridge tests
# ---------------------------------------------------------------------------


def _fake_sdk_module() -> SimpleNamespace:
    """Return a fake ``claude_agent_sdk`` namespace for offline tests."""
    tool_regs: list[dict[str, Any]] = []
    server_calls: list[dict[str, Any]] = []

    def _fake_tool(name: str, description: str | None, parameters_json_schema: dict[str, Any]):
        tool_regs.append(
            dict(name=name, description=description, schema=parameters_json_schema)
        )

        def _decorator(fn):
            return fn

        return _decorator

    def _fake_create_sdk_mcp_server(name: str, tools: list):
        server_calls.append(dict(name=name, tools=list(tools)))
        return SimpleNamespace()

    class _FakeTextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class _FakeAssistantMessage:
        def __init__(self, text: str) -> None:
            self.content = [_FakeTextBlock(text)]

    class _FakeResultMessage:
        def __init__(self, usage: dict[str, int] | None = None) -> None:
            self.usage = usage
            self.result = None

    class _FakeClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    ns = SimpleNamespace()
    ns.tool = _fake_tool
    ns.create_sdk_mcp_server = _fake_create_sdk_mcp_server
    ns.TextBlock = _FakeTextBlock
    ns.AssistantMessage = _FakeAssistantMessage
    ns.ResultMessage = _FakeResultMessage
    ns.ClaudeAgentOptions = _FakeClaudeAgentOptions
    # Attach record-keeping
    ns._tool_regs = tool_regs
    ns._server_calls = server_calls
    return ns


def _install_fake_sdk(monkeypatch) -> SimpleNamespace:
    """Install a fake ``claude_agent_sdk`` module and return its namespace."""
    fake = _fake_sdk_module()
    sys.modules["claude_agent_sdk"] = fake
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return fake


# ---------------------------------------------------------------------------
# Tool-loop bridge tests
# ---------------------------------------------------------------------------


def _echo_sync(text: str) -> str:
    """Echo the input."""
    return text


def test_tool_agent_invokes_tool_and_returns_output(monkeypatch):
    """build_agent with tools returns a handle; run_sync invokes the SDK tool
    loop and the final text reaches .output (offline, monkeypatched SDK)."""
    fake = _install_fake_sdk(monkeypatch)

    canned_text = "the echo tool says: hello world"

    async def _fake_query(*, prompt, options):
        yield fake.AssistantMessage(canned_text)
        yield fake.ResultMessage({"input_tokens": 10, "output_tokens": 5})

    fake.query = _fake_query

    provider = ClaudeSDKProvider()
    handle = provider.build_agent(
        tier=Tier.CHEAP,
        system_prompt="You are a tester.",
        tools=[PydanticTool(_echo_sync, name="echo_sync")],
    )

    assert isinstance(handle, _SdkToolAgentHandle)

    result = handle.run_sync("Use the echo tool")
    assert isinstance(result, _SdkToolResult)
    assert result.output == canned_text
    assert isinstance(result.all_messages(), list)
    assert len(result.all_messages()) == 1
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5

    # Tool was registered with correct metadata
    assert len(fake._tool_regs) == 1
    assert fake._tool_regs[0]["name"] == "echo_sync"
    assert "Echo the input" in (fake._tool_regs[0]["description"] or "")
    assert fake._tool_regs[0]["schema"]["type"] == "object"

    # MCP server created with the tool
    assert len(fake._server_calls) == 1
    assert fake._server_calls[0]["name"] == "milltools"
    assert len(fake._server_calls[0]["tools"]) == 1

    handle.close()  # no-op, must not raise


def test_tool_definition_mapping_from_pydantic_tool(monkeypatch):
    """SDK tool registration receives correct name/description/schema from a
    pydantic-ai ``Tool`` with explicit metadata."""
    fake = _install_fake_sdk(monkeypatch)

    def _add(a: int, b: int = 0) -> int:
        """Add two numbers."""
        return a + b

    tool = PydanticTool(_add, name="adder", description="Returns a + b.")
    _convert_tools([tool])

    assert len(fake._tool_regs) == 1
    reg = fake._tool_regs[0]
    assert reg["name"] == "adder"
    assert reg["description"] == "Returns a + b."
    assert reg["schema"]["type"] == "object"
    assert "a" in reg["schema"]["properties"]
    assert "b" in reg["schema"]["properties"]
    assert reg["schema"]["properties"]["a"]["type"] == "integer"


def test_tool_definition_mapping_from_plain_callable(monkeypatch):
    """Plain callable is normalised to ``Tool`` and SDK registration still
    receives correct metadata derived from docstring + type hints."""
    fake = _install_fake_sdk(monkeypatch)

    def greet(name: str, enthusiastic: bool = False) -> str:
        """Return a greeting for *name*.

        If *enthusiastic*, uppercase the result.
        """
        msg = f"Hello {name}"
        return msg.upper() if enthusiastic else msg

    _convert_tools([greet])

    assert len(fake._tool_regs) == 1
    reg = fake._tool_regs[0]
    assert reg["name"] == "greet"
    assert "Return a greeting" in (reg["description"] or "")
    assert reg["schema"]["type"] == "object"
    assert "name" in reg["schema"]["properties"]
    assert reg["schema"]["properties"]["name"]["type"] == "string"


def test_notools_path_returns_agent_handle():
    """When *tools* is None/empty, ``build_agent`` returns a standard
    ``AgentHandle`` wrapping a pydantic-ai ``Agent`` — the existing
    no-tools path is unchanged."""
    provider = ClaudeSDKProvider()
    handle = provider.build_agent(
        tier=Tier.CHEAP, system_prompt="You are helpful.", tools=None
    )
    # With no tools the super().build_agent() path wraps a pydantic-ai Agent.
    assert isinstance(handle, AgentHandle)
    assert handle._agent is not None  # type: ignore[attr-defined]
    handle.close()


def test_tools_empty_list_also_returns_agent_handle():
    """Empty tools list is falsy → delegates to the no-tools AgentHandle path."""
    provider = ClaudeSDKProvider()
    handle = provider.build_agent(
        tier=Tier.CHEAP, system_prompt="You are helpful.", tools=[]
    )
    assert isinstance(handle, AgentHandle)
    handle.close()


# ---------------------------------------------------------------------------
# Live tool round-trip (deselected by default)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_live_tool_round_trip():
    """Live: one real tool call end-to-end with the Claude Agent SDK.

    Skips when the ``claude`` CLI / SDK is unavailable or not logged in.
    """
    import shutil

    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found on PATH")

    try:
        import claude_agent_sdk  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        pytest.skip("claude_agent_sdk import failed (SDK not installed)")

    provider = ClaudeSDKProvider()

    def _echo(text: str) -> str:
        return text

    handle = provider.build_agent(
        tier=Tier.CHEAP,
        system_prompt="You are a QA bot. When asked to echo, call the echo "
        'tool and then repeat exactly what it returned prefixed with "ECHO: ".',
        tools=[PydanticTool(_echo)],
    )

    result = handle.run_sync("Use the echo tool to repeat: hello42")
    assert "hello42" in str(result.output).lower()
    handle.close()
