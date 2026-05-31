"""Live Langfuse round-trip: configure export, run a real model call, then query
the Langfuse API to confirm the trace landed *with cost*.

On-demand only (``live`` marker). Skips unless both ``LANGFUSE_PUBLIC_KEY`` /
``LANGFUSE_SECRET_KEY`` and ``OPENROUTER_API_KEY`` are set. Uses the cheap tier
and a tiny ``max_tokens`` to keep the spend negligible.
"""

from __future__ import annotations

import base64
import os
import time
import uuid

import pytest


def _langfuse_creds() -> tuple[str | None, str | None, str]:
    return (
        os.environ.get("LANGFUSE_PUBLIC_KEY"),
        os.environ.get("LANGFUSE_SECRET_KEY"),
        os.environ.get("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
    )


def _require() -> None:
    pk, sk, _ = _langfuse_creds()
    if not (pk and sk):
        pytest.skip("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")


def _langfuse_traces(session_id: str) -> list[dict] | None:
    """GET the Langfuse traces for *session_id*, or None on a failed request."""
    import httpx

    pk, sk, base = _langfuse_creds()
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    with httpx.Client(timeout=20) as client:
        resp = client.get(
            f"{base.rstrip('/')}/api/public/traces",
            params={"sessionId": session_id, "limit": 10},
            headers={"Authorization": f"Basic {auth}"},
        )
    if resp.status_code != 200:
        return None
    return resp.json().get("data", [])


@pytest.mark.live
def test_langfuse_trace_roundtrip_has_cost() -> None:
    """A real provider call, grouped under a unique session, produces a Langfuse
    trace whose ``totalCost`` is populated from the per-call cost the model
    stamps on the span."""
    _require()

    from robotsix_llmio.core import (
        Tier,
        flush_tracing,
        langfuse_session,
        setup_langfuse_tracing,
    )
    from robotsix_llmio.openrouter_deepseek import OpenRouterDeepseekProvider

    assert setup_langfuse_tracing() is True, "tracing should configure with creds"

    session_id = f"llmio-livetest-{uuid.uuid4().hex[:12]}"
    provider = OpenRouterDeepseekProvider()
    agent = provider.build_agent(
        tier=Tier.CHEAP,
        system_prompt="You are concise. Answer with just the number.",
        name="tracing-livetest",
    )
    try:
        with langfuse_session(session_id):
            result = provider.call_with_retry(
                lambda: agent.run_sync(
                    "What is 2+2?", model_settings={"max_tokens": 20}
                )
            )
        assert "4" in str(result.output)
    finally:
        agent.close()

    flush_tracing()

    # Langfuse ingestion is asynchronous — poll for the trace to appear.
    traces: list[dict] | None = None
    for _ in range(15):
        traces = _langfuse_traces(session_id)
        if traces:
            break
        time.sleep(4)

    assert traces, f"no Langfuse trace for session {session_id!r} after polling"
    total_cost = sum(float(t.get("totalCost") or 0) for t in traces)
    assert total_cost > 0, (
        f"trace landed but totalCost={total_cost} (expected > 0 — cost should "
        f"flow from the model span's langfuse.observation.cost_details)"
    )


def _require_claude() -> None:
    pk, sk, _ = _langfuse_creds()
    if not (pk and sk):
        pytest.skip("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")
    import importlib.util
    import shutil

    if importlib.util.find_spec("claude_agent_sdk") is None:
        pytest.skip("claude_agent_sdk not installed")
    if shutil.which("claude") is None:
        pytest.skip("`claude` CLI not on PATH (run `claude login`)")


@pytest.mark.live
def test_langfuse_trace_roundtrip_claude_sdk_has_cost() -> None:
    """claude_sdk provider (subscription auth) — a traced run lands in Langfuse
    with cost.

    Uses the no-tools path (``output_type=str``, no tools) so the run goes
    through the instrumented pydantic-ai ``Agent``; the SDK tool-loop path
    bypasses instrumentation. Cost comes from the SDK's ``total_cost_usd``
    estimate, which the model stamps on the span via ``record_cost``.
    """
    _require_claude()

    from robotsix_llmio.claude_sdk import ClaudeSDKProvider
    from robotsix_llmio.core import (
        Tier,
        flush_tracing,
        langfuse_session,
        setup_langfuse_tracing,
    )

    assert setup_langfuse_tracing() is True, "tracing should configure with creds"

    session_id = f"llmio-livetest-claude-{uuid.uuid4().hex[:12]}"
    provider = ClaudeSDKProvider()
    agent = provider.build_agent(
        tier=Tier.CHEAP,
        system_prompt="You are concise. Answer with just the number.",
        output_type=str,
        name="tracing-livetest-claude",
    )
    try:
        with langfuse_session(session_id):
            result = provider.call_with_retry(
                lambda: agent.run_sync("What is 2+2?")
            )
        assert "4" in str(result.output)
    finally:
        agent.close()

    flush_tracing()

    traces: list[dict] | None = None
    for _ in range(15):
        traces = _langfuse_traces(session_id)
        if traces:
            break
        time.sleep(4)

    assert traces, f"no Langfuse trace for session {session_id!r} after polling"
    total_cost = sum(float(t.get("totalCost") or 0) for t in traces)
    assert total_cost > 0, (
        f"trace landed but totalCost={total_cost} (expected > 0 — claude_sdk "
        f"records total_cost_usd on the span)"
    )
