"""Live integration tests for DeepSeek models over OpenRouter.

All tests are gated behind the ``live`` pytest marker and skip when
``OPENROUTER_API_KEY`` is not set.  They use low ``max_tokens`` to keep
API costs negligible.
"""

from __future__ import annotations

import os

import pytest

from robotsix_llmio.core.provider import Tier
from robotsix_llmio.openrouter_deepseek.provider import OpenRouterDeepseekProvider


def _require_key() -> None:
    """Skip the current test when ``OPENROUTER_API_KEY`` is not set."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")


def _make_provider() -> OpenRouterDeepseekProvider:
    """Build a provider using the real ``OPENROUTER_API_KEY``."""
    return OpenRouterDeepseekProvider()


def _echo(text: str) -> str:
    """Echo the input text back — a trivial tool for integration tests."""
    return text


# ---------------------------------------------------------------------------
# DeepSeek V4 Pro  (Tier.DEFAULT, reasoning enabled)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_pro_basic_text() -> None:
    """Trivial text completion with the capable tier."""
    _require_key()
    provider = _make_provider()
    agent = provider.build_agent(
        tier=Tier.DEFAULT,
        system_prompt="You are a helpful assistant. Answer concisely.",
    )
    try:
        result = agent.run_sync(
            "What is 2+2? Answer with just the number.",
            model_settings={"max_tokens": 50},
        )
        assert result.output is not None
        assert len(str(result.output)) > 0
        assert "4" in str(result.output)
    finally:
        agent.close()


@pytest.mark.live
def test_pro_tool_usage() -> None:
    """Tool call with reasoning active — verifies thinking-mode tool echo."""
    _require_key()
    provider = _make_provider()
    agent = provider.build_agent(
        tier=Tier.DEFAULT,
        system_prompt="You are a helpful assistant. Use tools when asked.",
        tools=[_echo],
    )
    try:
        result = agent.run_sync(
            "Use the echo tool to repeat the exact text: 'hello world'",
            model_settings={"max_tokens": 200},
        )
        output = str(result.output).lower()
        assert "hello world" in output

        # Verify at least one ToolCallPart is present in the message history.
        from pydantic_ai.messages import ToolCallPart

        messages = result.all_messages()
        tool_calls = [
            part
            for msg in messages
            for part in getattr(msg, "parts", [])
            if isinstance(part, ToolCallPart)
        ]
        assert len(tool_calls) > 0, "Expected at least one tool call"
    finally:
        agent.close()


@pytest.mark.live
def test_pro_thinking_tool_mix() -> None:
    """Thinking + tool, multi-turn — works WITHOUT any reasoning remap.

    This documents why the DeepSeek reasoning round-trip tweak was removed: a
    thinking+tool conversation pinned to DeepSeek first-party completes with no
    HTTP 400. pydantic-ai round-trips reasoning natively, so the layer needs no
    ``reasoning_details`` echo. Asserts thinking is genuinely active (a
    ``ThinkingPart`` on the tool-call response) so the 400 precondition is met
    yet still does not fire. If this ever starts 400ing, the tweak is back on
    the table.
    """
    _require_key()
    provider = _make_provider()
    agent = provider.build_agent(
        tier=Tier.DEFAULT,
        system_prompt="You are a helpful assistant. Use tools when helpful.",
        tools=[_echo],
    )
    try:
        result = agent.run_sync(
            "Use the echo tool to repeat the greeting 'Hello from DeepSeek!', "
            "then tell me what you did.",
            model_settings={"max_tokens": 300},
        )
        output = str(result.output)
        assert "Hello from DeepSeek" in output

        from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart

        messages = result.all_messages()
        responses = [m for m in messages if isinstance(m, ModelResponse)]
        tool_calls = [
            p for m in responses for p in m.parts if isinstance(p, ToolCallPart)
        ]
        thinking = [
            p for m in responses for p in m.parts if isinstance(p, ThinkingPart)
        ]
        assert len(tool_calls) > 0, "Expected at least one tool call"
        # Thinking must be active — otherwise this wouldn't exercise the 400
        # precondition at all (reaching here without a 400 is the whole point).
        assert len(thinking) > 0, "Expected reasoning (ThinkingPart) to be active"
    finally:
        agent.close()


# ---------------------------------------------------------------------------
# DeepSeek V4 Flash  (Tier.CHEAP, reasoning disabled)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_flash_basic_text() -> None:
    """Trivial text completion with the cheap/fast tier."""
    _require_key()
    provider = _make_provider()
    agent = provider.build_agent(
        tier=Tier.CHEAP,
        system_prompt="You are a helpful assistant. Answer concisely.",
    )
    try:
        result = agent.run_sync(
            "What is 2+2? Answer with just the number.",
            model_settings={"max_tokens": 50},
        )
        assert result.output is not None
        assert len(str(result.output)) > 0
        assert "4" in str(result.output)
    finally:
        agent.close()


@pytest.mark.live
def test_flash_tool_usage() -> None:
    """Tool call with reasoning disabled (cheap tier).

    The pin sets ``reasoning: {enabled: False}``, so DeepSeek emits no
    reasoning at all — there is nothing to round-trip and no ``ThinkingPart``.
    The tool call still works.
    """
    _require_key()
    provider = _make_provider()
    agent = provider.build_agent(
        tier=Tier.CHEAP,
        system_prompt="You are a helpful assistant. Use tools when asked.",
        tools=[_echo],
    )
    try:
        result = agent.run_sync(
            "Use the echo tool to repeat the exact text: 'hello from flash'",
            model_settings={"max_tokens": 200},
        )
        output = str(result.output).lower()
        assert "hello from flash" in output

        from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart

        responses = [m for m in result.all_messages() if isinstance(m, ModelResponse)]
        tool_calls = [
            p for m in responses for p in m.parts if isinstance(p, ToolCallPart)
        ]
        thinking = [
            p for m in responses for p in m.parts if isinstance(p, ThinkingPart)
        ]
        assert len(tool_calls) > 0, "Expected at least one tool call"
        # Reasoning is disabled for the cheap tier — no thinking should appear.
        assert thinking == [], "Cheap tier disables reasoning; expected no thinking"
    finally:
        agent.close()


# ---------------------------------------------------------------------------
# Regression: resume from a pending tool result (the reasoning round-trip 400)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_pro_resume_from_pending_tool_return_does_not_400() -> None:
    """Resuming a thinking-mode conversation from a PENDING tool result must not
    raise DeepSeek's ``reasoning_content`` 400.

    Repro of mill ticket 64e6 ("The `reasoning_content` in the thinking mode
    must be passed back to the API", model deepseek/deepseek-v4-pro). The
    capable tier runs in thinking mode; when ``message_history`` ends with an
    assistant ``tool_call`` followed by its ``tool_return`` and the run
    continues (no new user prompt), DeepSeek requires the assistant tool-call
    message to carry ``reasoning_content``. pydantic-ai does not send it back in
    this reconstructed-history shape, so the request 400s — captured live both
    with and without a ``ThinkingPart`` on the reconstructed assistant turn.

    mill produces exactly this shape whenever it replays/compacts/pre-seeds a
    history that ends at a tool_return (pause-and-resume mid tool-loop,
    ``conversation_state`` replay, history compaction). FAILS today; the fix is
    to restore the DeepSeek reasoning round-trip in the openrouter_deepseek
    layer so the assistant tool-call message carries reasoning_content.
    """
    _require_key()
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )

    provider = _make_provider()
    agent = provider.build_agent(
        tier=Tier.DEFAULT,
        system_prompt="Use the echo tool when asked.",
        tools=[_echo],
    )
    history = [
        ModelRequest(parts=[UserPromptPart(content="Echo the word 'ping'.")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="_echo", args={"text": "ping"}, tool_call_id="c1"
                )
            ]
        ),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="_echo", content="ping", tool_call_id="c1")]
        ),
    ]
    try:
        # Continue from the pending tool result (prompt=None). Must NOT 400.
        result = agent.run_sync(
            None, message_history=history, model_settings={"max_tokens": 200}
        )
        assert result.output is not None
    finally:
        agent.close()
