"""Claude Agent SDK transport — prompt rendering, usage mapping, guards,
transient. Offline only: the live single-turn call is exercised separately
(needs the `claude` CLI + login)."""

from __future__ import annotations

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

from robotsix_llmio.claude_sdk.model import (
    ClaudeSDKModel,
    _map_usage,
    render_prompt,
)
from robotsix_llmio.claude_sdk.transient import is_claude_sdk_transient


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
