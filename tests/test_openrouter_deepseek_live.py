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
# DeepSeek V4 Pro  (Tier.DEFAULT, reasoning enabled, echo_reasoning=True)
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
        assert result.data is not None
        assert len(str(result.data)) > 0
        assert "4" in str(result.data)
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
        output = str(result.data).lower()
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
    """Reasoning + tool round-trip — the reliability case.

    Exercises the echo path: reasoning_details must survive a tool-call
    turn so the next request doesn't trigger the thinking-mode 400.
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
        output = str(result.data)
        assert "Hello from DeepSeek" in output

        from pydantic_ai.messages import ModelResponse, ToolCallPart

        messages = result.all_messages()
        tool_call_msgs: list[ModelResponse] = []
        for msg in messages:
            if isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if isinstance(part, ToolCallPart):
                        tool_call_msgs.append(msg)
                        break

        assert len(tool_call_msgs) > 0, "Expected at least one tool call"

        # Every ModelResponse that carries a tool call must have
        # reasoning_details echoed back (the round-trip echo path).
        for msg in tool_call_msgs:
            rd = (msg.provider_details or {}).get("reasoning_details")
            assert rd is not None, (
                f"Expected reasoning_details on tool-call ModelResponse, "
                f"got provider_details={msg.provider_details}"
            )
            assert len(rd) > 0, "reasoning_details must be non-empty"
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
        assert result.data is not None
        assert len(str(result.data)) > 0
        assert "4" in str(result.data)
    finally:
        agent.close()


@pytest.mark.live
def test_flash_tool_usage() -> None:
    """Tool call with reasoning disabled — verifies the strip path.

    Since reasoning is disabled for the cheap tier, reasoning_details
    must never appear on tool-call messages (no thinking-mode mix-400).
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
        output = str(result.data).lower()
        assert "hello from flash" in output

        from pydantic_ai.messages import ModelResponse, ToolCallPart

        messages = result.all_messages()
        tool_call_msgs: list[ModelResponse] = []
        for msg in messages:
            if isinstance(msg, ModelResponse):
                for part in msg.parts:
                    if isinstance(part, ToolCallPart):
                        tool_call_msgs.append(msg)
                        break

        assert len(tool_call_msgs) > 0, "Expected at least one tool call"

        # Flash / reasoning-disabled tier: no reasoning_details anywhere.
        for msg in tool_call_msgs:
            rd = (msg.provider_details or {}).get("reasoning_details")
            assert rd is None, (
                f"Expected no reasoning_details on flash tool-call message, got {rd}"
            )
    finally:
        agent.close()
