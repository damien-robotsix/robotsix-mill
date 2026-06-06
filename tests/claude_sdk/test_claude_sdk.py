"""Claude Agent SDK transport — prompt rendering, usage mapping, guards,
transient, and tool-loop bridge.  Offline only: the live single-turn and
tool round-trip tests are exercised separately (need the ``claude`` CLI
+ login)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel as _BM

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
    _chat_messages_input,
    _convert_tools,
    _extract_json_object,
    _parse_output,
    _SdkToolAgentHandle,
    _SdkToolResult,
)
from robotsix_llmio.claude_sdk.transient import (
    is_claude_sdk_transient,
    is_claude_sdk_turn_limit,
)
from robotsix_llmio.core.agent import AgentHandle
from robotsix_llmio.core.provider import Tier

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
        usage: ClassVar = {
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
        usage: ClassVar = {"input_tokens": 4}

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


# --- turn-limit: hard failure, never retried -------------------------------


def test_turn_limit_message_detected_and_not_transient():
    e = Exception(
        "Claude Code returned an error result: Reached maximum number of turns (8)"
    )
    assert is_claude_sdk_turn_limit(e) is True
    # Must NOT be retried — retrying would just loop to the cap again.
    assert is_claude_sdk_transient(e) is False


def test_turn_limit_wins_even_when_wrapped_as_process_error():
    # ProcessError is normally transient; the turn-limit guard must win so we
    # fail loudly instead of burning retries.
    class ProcessError(Exception):
        pass

    e = ProcessError("CLI exited 1: Reached maximum number of turns (8)")
    assert is_claude_sdk_transient(e) is False


def test_turn_limit_error_type_detected_and_not_transient():
    from robotsix_llmio.claude_sdk.model import ClaudeSDKTurnLimitError

    e = ClaudeSDKTurnLimitError("hit the cap")
    assert is_claude_sdk_turn_limit(e) is True
    assert is_claude_sdk_transient(e) is False


def test_non_turn_limit_runtime_error_unaffected():
    assert is_claude_sdk_turn_limit(RuntimeError("something else")) is False


# --- per-call wall-clock timeout: stalled run fails fast + is retryable ------


def test_query_timeout_is_transient_but_not_turn_limit():
    from robotsix_llmio.claude_sdk.model import ClaudeSDKQueryTimeout

    e = ClaudeSDKQueryTimeout("stalled")
    # A stall re-runs cleanly, so it must be retried...
    assert is_claude_sdk_transient(e) is True
    # ...but it is NOT the (never-retried) turn-cap hard failure.
    assert is_claude_sdk_turn_limit(e) is False


def test_tool_loop_query_timeout_raises_claude_sdk_query_timeout(monkeypatch):
    """A query() that stalls past SDK_QUERY_TIMEOUT raises ClaudeSDKQueryTimeout
    (the tool-loop path), instead of hanging on the SDK's own backstop."""
    from robotsix_llmio.claude_sdk.model import ClaudeSDKQueryTimeout
    from robotsix_llmio.core import constants

    fake = _install_fake_sdk(monkeypatch)

    async def _hanging_query(*, prompt, options):
        await asyncio.sleep(30)  # never completes within the cap
        yield fake.ResultMessage()  # pragma: no cover — cancelled first

    fake.query = _hanging_query
    monkeypatch.setattr(constants, "SDK_QUERY_TIMEOUT", 0.05)

    provider = ClaudeSDKProvider()
    handle = provider.build_agent(
        tier=Tier.CHEAP,
        system_prompt="sys",
        tools=[PydanticTool(_echo_sync, name="echo_sync")],
    )
    with pytest.raises(ClaudeSDKQueryTimeout):
        handle.run_sync("do something")
    handle.close()


def test_single_turn_invoke_query_timeout_raises(monkeypatch):
    """The no-tools single-turn path (ClaudeSDKModel._invoke) also enforces the
    per-call wall-clock cap."""
    from robotsix_llmio.claude_sdk.model import ClaudeSDKModel, ClaudeSDKQueryTimeout
    from robotsix_llmio.core import constants

    fake = _install_fake_sdk(monkeypatch)

    async def _hanging_query(*, prompt, options):
        await asyncio.sleep(30)
        yield fake.ResultMessage()  # pragma: no cover — cancelled first

    fake.query = _hanging_query
    monkeypatch.setattr(constants, "SDK_QUERY_TIMEOUT", 0.05)

    model = ClaudeSDKModel("haiku")
    with pytest.raises(ClaudeSDKQueryTimeout):
        asyncio.run(model._invoke("hi", None))


# --- turn cap: single source, generous for injected-MCP-tool loops ---------


def test_tool_handle_uses_shared_max_turns_cap():
    from robotsix_llmio.claude_sdk.model import _MAX_TURNS

    handle = _SdkToolAgentHandle("opus", "sys", None, [], str)
    assert handle._max_turns == _MAX_TURNS  # single source — paths can't drift
    assert _MAX_TURNS >= 100  # generous cap so genuine tool loops don't trip it


# ---------------------------------------------------------------------------
# Helpers for tool-loop bridge tests
# ---------------------------------------------------------------------------


def _fake_sdk_module() -> SimpleNamespace:
    """Return a fake ``claude_agent_sdk`` namespace for offline tests."""
    tool_regs: list[dict[str, Any]] = []
    server_calls: list[dict[str, Any]] = []

    def _fake_tool(
        name: str, description: str | None, parameters_json_schema: dict[str, Any]
    ):
        tool_regs.append(
            {"name": name, "description": description, "schema": parameters_json_schema}
        )

        def _decorator(fn):
            return fn

        return _decorator

    def _fake_create_sdk_mcp_server(name: str, tools: list):
        server_calls.append({"name": name, "tools": list(tools)})
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


# ---------------------------------------------------------------------------
# run_sync/run kwargs: honor message_history, warn on the rest (never silent)
# ---------------------------------------------------------------------------


def _capturing_query(fake, captured: dict):
    """A fake SDK ``query`` that records the prompt it was handed."""

    async def _q(*, prompt, options):
        captured["prompt"] = prompt
        yield fake.AssistantMessage("done")
        yield fake.ResultMessage({"input_tokens": 1, "output_tokens": 1})

    return _q


def _tool_handle():
    return ClaudeSDKProvider().build_agent(
        tier=Tier.CHEAP,
        system_prompt="sys",
        tools=[PydanticTool(_echo_sync, name="echo_sync")],
    )


def test_tool_run_sync_honors_message_history(monkeypatch):
    """A message_history passed to the tool-loop run_sync is folded into the
    prompt (prior transcript + the new turn), so the caller keeps context."""
    fake = _install_fake_sdk(monkeypatch)
    captured: dict = {}
    fake.query = _capturing_query(fake, captured)

    handle = _tool_handle()
    history = [
        ModelRequest(parts=[UserPromptPart(content="first question")]),
        ModelResponse(parts=[TextPart(content="prior answer")]),
    ]
    handle.run_sync("the new turn", message_history=history)

    prompt = captured["prompt"]
    assert "first question" in prompt
    assert "prior answer" in prompt
    assert prompt.endswith("User: the new turn")  # new turn appended last
    handle.close()


def test_tool_run_sync_without_history_sends_prompt_verbatim(monkeypatch):
    fake = _install_fake_sdk(monkeypatch)
    captured: dict = {}
    fake.query = _capturing_query(fake, captured)

    handle = _tool_handle()
    handle.run_sync("just this")
    assert captured["prompt"] == "just this"  # no history → no transcript wrap
    handle.close()


def test_tool_run_sync_warns_on_unsupported_kwargs(monkeypatch, caplog):
    """Unsupported run kwargs (usage_limits, model_settings) are warned about,
    not silently dropped — and the run still completes."""
    fake = _install_fake_sdk(monkeypatch)
    captured: dict = {}
    fake.query = _capturing_query(fake, captured)

    handle = _tool_handle()
    with caplog.at_level(logging.WARNING, logger="robotsix_llmio.claude_sdk"):
        result = handle.run_sync(
            "hi", usage_limits="L", model_settings={"temperature": 0}
        )

    assert result.output == "done"  # run still works
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "usage_limits" in warned
    assert "model_settings" in warned
    handle.close()


def test_tool_async_run_honors_message_history(monkeypatch):
    """The async run() path threads message_history through the same way."""
    fake = _install_fake_sdk(monkeypatch)
    captured: dict = {}
    fake.query = _capturing_query(fake, captured)

    handle = _tool_handle()
    history = [ModelRequest(parts=[UserPromptPart(content="earlier ctx")])]
    asyncio.run(handle.run("now", message_history=history))

    assert "earlier ctx" in captured["prompt"]
    assert captured["prompt"].endswith("User: now")
    handle.close()


# ---------------------------------------------------------------------------
# Tracing: the system prompt (sent to the SDK) is surfaced on the generation
# ---------------------------------------------------------------------------


def test_chat_messages_input_renders_system_and_user():
    raw = _chat_messages_input("be terse", "hello there")
    msgs = json.loads(raw)
    assert msgs == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hello there"},
    ]


def test_generation_span_input_includes_system_prompt(monkeypatch):
    """End-to-end: the ``chat`` generation span records system + user as chat
    messages, so the system prompt is visible in the trace (not just input)."""
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    import robotsix_llmio.claude_sdk.provider as prov

    exporter = InMemorySpanExporter()
    provider_obj = TracerProvider()
    provider_obj.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider_obj.get_tracer("test")
    # Route the module's spans to our isolated, recording provider (the offline
    # suite installs no global TracerProvider).
    monkeypatch.setattr(prov, "get_tracer", lambda _name: tracer)

    fake = _install_fake_sdk(monkeypatch)

    async def _fake_query(*, prompt, options):
        yield fake.AssistantMessage("the answer")
        yield fake.ResultMessage({"input_tokens": 1, "output_tokens": 1})

    fake.query = _fake_query

    handle = ClaudeSDKProvider().build_agent(
        tier=Tier.CHEAP,
        system_prompt="SYS_MARKER stay precise",
        tools=[PydanticTool(_echo_sync, name="echo_sync")],
    )
    handle.run_sync("USER_MARKER hi")
    handle.close()

    spans = exporter.get_finished_spans()

    def _input_messages(predicate) -> list:
        matched = [s for s in spans if predicate(s)]
        assert matched, f"no matching span in {[s.name for s in spans]}"
        return json.loads(matched[0].attributes["langfuse.observation.input"])

    # The child generation span carries system + user...
    chat = _input_messages(lambda s: s.name.startswith("chat "))
    assert chat[0]["role"] == "system" and "SYS_MARKER" in chat[0]["content"]
    assert chat[1]["role"] == "user" and "USER_MARKER" in chat[1]["content"]

    # ...and so does the root agent-run span (which becomes the trace), so the
    # system prompt is visible at the trace root, not only on the generation.
    root = _input_messages(
        lambda s: s.attributes.get("gen_ai.operation.name") == "invoke_agent"
    )
    assert root[0]["role"] == "system" and "SYS_MARKER" in root[0]["content"]
    assert root[1]["role"] == "user" and "USER_MARKER" in root[1]["content"]


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
        pass
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


@pytest.mark.live
def test_live_query_timeout_fires_against_real_cli(monkeypatch):
    """Live: a real ``query()`` against the ``claude`` CLI subprocess that is
    capped at a sub-spawn-time wall clock raises ``ClaudeSDKQueryTimeout`` (not
    a hang) — proving the asyncio.wait_for cancellation path works end-to-end
    against the real subprocess, not just the offline fake."""
    import shutil

    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found on PATH")

    from robotsix_llmio.claude_sdk.model import ClaudeSDKQueryTimeout
    from robotsix_llmio.core import constants

    # 1ms cap — far below the time to even spawn the CLI, so it must trip.
    monkeypatch.setattr(constants, "SDK_QUERY_TIMEOUT", 0.001)

    provider = ClaudeSDKProvider()

    def _echo(text: str) -> str:
        return text

    handle = provider.build_agent(
        tier=Tier.CHEAP,
        system_prompt="You are a QA bot.",
        tools=[PydanticTool(_echo)],
    )
    with pytest.raises(ClaudeSDKQueryTimeout):
        handle.run_sync("Use the echo tool to repeat: hello42")
    handle.close()


@pytest.mark.live
def test_live_tool_run_sync_honors_message_history():
    """Live: a real tool-loop run_sync recalls context supplied only via
    message_history — proving the folded-in transcript actually reaches the
    model, not just the offline fake."""
    import shutil

    if shutil.which("claude") is None:
        pytest.skip("claude CLI not found on PATH")

    def _noop(text: str) -> str:
        """A trivial tool so this exercises the tool-loop path."""
        return text

    handle = ClaudeSDKProvider().build_agent(
        tier=Tier.CHEAP,
        system_prompt="You are a precise assistant. Answer tersely.",
        tools=[PydanticTool(_noop, name="noop")],
    )

    # The fact lives ONLY in the prior turn passed as message_history.
    history = [
        ModelRequest(
            parts=[UserPromptPart(content="My favorite number is 4273. Acknowledge.")]
        ),
        ModelResponse(parts=[TextPart(content="Acknowledged: 4273.")]),
    ]
    result = handle.run_sync(
        "What is my favorite number? Reply with just the digits.",
        message_history=history,
    )
    assert "4273" in str(result.output)
    handle.close()


# --- structured-output JSON extraction (prose + fenced / stray braces) -------


class _Verdict(_BM):
    verdict: str
    auto_merge_eligible: bool = False


def test_parse_output_str_passthrough():
    assert _parse_output("anything", str) == "anything"


def test_extract_clean_json():
    assert _extract_json_object('{"verdict": "APPROVE"}') == {"verdict": "APPROVE"}


def test_extract_fenced_json_after_prose():
    # The 402b shape: prose preamble, then a ```json fence with the verdict.
    text = (
        "Looking at this review.\n\n## Analysis\nlooks good.\n\n"
        '```json\n{"verdict": "APPROVE", "auto_merge_eligible": true}\n```\n'
    )
    v = _parse_output(text, _Verdict)
    assert isinstance(v, _Verdict)
    assert v.verdict == "APPROVE" and v.auto_merge_eligible is True


def test_extract_ignores_stray_prose_brace():
    # A stray `{...}` in prose must NOT derail extraction of the real object
    # (the old greedy re.search anchored on the first brace and failed).
    text = (
        "The `{verified_proposals}` kwarg is passed through. Verdict below:\n"
        '```json\n{"verdict": "REQUEST_CHANGES"}\n```'
    )
    v = _parse_output(text, _Verdict)
    assert v.verdict == "REQUEST_CHANGES"


def test_extract_prose_wrapped_json_no_fence():
    # No fence, just prose then a JSON object with nested structures.
    text = (
        'Here is my verdict: {"verdict": "APPROVE", "auto_merge_eligible": false} done.'
    )
    v = _parse_output(text, _Verdict)
    assert v.verdict == "APPROVE"


def test_extract_picks_last_valid_object():
    # An earlier non-matching object (e.g. an example) then the real one.
    text = (
        'Example shape: {"foo": 1}\n\nActual:\n'
        '```json\n{"verdict": "NEEDS_DISCUSSION"}\n```'
    )
    v = _parse_output(text, _Verdict)
    assert v.verdict == "NEEDS_DISCUSSION"


def test_extract_no_json_falls_back_to_text():
    assert _parse_output("no json at all here", _Verdict) == "no json at all here"


def test_extract_nested_object_captured_whole():
    text = '```json\n{"verdict": "APPROVE", "nested": {"a": {"b": [1,2]}}}\n```'
    assert _extract_json_object(text) == {
        "verdict": "APPROVE",
        "nested": {"a": {"b": [1, 2]}},
    }
