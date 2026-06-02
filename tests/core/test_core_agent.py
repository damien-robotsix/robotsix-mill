"""Core agent module — AgentHandle delegation/cleanup and build_agent wiring.

Provider-agnostic: these tests mock both the underlying pydantic-ai Agent and
the HTTP client so no network or pydantic-ai-specific runtime behaviour is
exercised.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from robotsix_llmio.core import agent as agent_module
from robotsix_llmio.core.agent import AgentHandle, _safe_close, build_agent


# --- AgentHandle.__getattr__ delegation -------------------------------------


def test_agenthandle_getattr_proxies_to_underlying_agent():
    """Attribute access falls through to the wrapped agent."""

    class _MockAgent:
        run_sync = "the-run-sync-callable"
        name = "inner-name"

        def some_method(self) -> str:
            return "called"

    inner = _MockAgent()
    handle = AgentHandle(inner, http_client=None)

    # Plain attribute is proxied through __getattr__.
    assert handle.run_sync == "the-run-sync-callable"
    assert handle.name == "inner-name"
    # Bound methods proxy too.
    assert handle.some_method() == "called"


def test_agenthandle_getattr_raises_for_missing_attribute():
    """When the underlying agent lacks an attribute, AttributeError surfaces."""
    handle = AgentHandle(object(), http_client=None)
    with pytest.raises(AttributeError):
        handle.definitely_not_a_real_attr  # noqa: B018


def test_agenthandle_own_attrs_are_not_proxied():
    """Attributes defined on AgentHandle itself short-circuit __getattr__."""

    class _MockAgent:
        # Has the same attribute names as AgentHandle internals; if delegation
        # were over-eager it would return these instead.
        _agent = "inner-agent-marker"
        _http_client = "inner-http-marker"

    inner = _MockAgent()
    handle = AgentHandle(inner, http_client="real-http")

    # AgentHandle's own slots win over delegation for these names.
    assert handle._agent is inner
    assert handle._http_client == "real-http"


# --- AgentHandle.close() ---------------------------------------------------


def test_agenthandle_close_calls_close_async_client(monkeypatch):
    """close() forwards the HTTP client to _close_async_client."""
    calls: list[Any] = []

    def fake_close(client: Any) -> None:
        calls.append(client)

    monkeypatch.setattr(agent_module, "_close_async_client", fake_close)

    sentinel_client = object()
    handle = AgentHandle(agent=object(), http_client=sentinel_client)
    handle.close()

    assert calls == [sentinel_client]
    # The handle drops its reference so a second close() is a no-op.
    assert handle._http_client is None


def test_agenthandle_close_is_idempotent(monkeypatch):
    """Calling close() twice must not double-close the HTTP client."""
    calls: list[Any] = []

    def fake_close(client: Any) -> None:
        calls.append(client)

    monkeypatch.setattr(agent_module, "_close_async_client", fake_close)

    handle = AgentHandle(agent=object(), http_client=object())
    handle.close()
    handle.close()
    handle.close()

    assert len(calls) == 1


def test_agenthandle_close_noop_when_http_client_already_none(monkeypatch):
    """If the handle was constructed without an HTTP client, close() is a no-op."""
    calls: list[Any] = []

    def fake_close(client: Any) -> None:
        calls.append(client)

    monkeypatch.setattr(agent_module, "_close_async_client", fake_close)

    handle = AgentHandle(agent=object(), http_client=None)
    handle.close()

    assert calls == []


# --- _safe_close -----------------------------------------------------------


def test_safe_close_invokes_close_when_present():
    """_safe_close calls a present close() method exactly once."""
    calls = {"n": 0}

    class _Agent:
        def close(self) -> None:
            calls["n"] += 1

    _safe_close(_Agent())
    assert calls["n"] == 1


def test_safe_close_noop_when_close_missing():
    """No close attribute → silently no-op, no AttributeError."""

    class _AgentWithoutClose:
        pass

    # Must not raise.
    _safe_close(_AgentWithoutClose())


def test_safe_close_swallows_close_exception():
    """If close() raises, _safe_close swallows the exception."""

    class _ExplodingAgent:
        def close(self) -> None:
            raise RuntimeError("boom")

    # Must not raise — _safe_close exists precisely for finally/__del__ paths.
    _safe_close(_ExplodingAgent())


def test_safe_close_handles_close_set_to_none():
    """A ``close`` attribute that is explicitly None must not be invoked."""

    class _AgentNoneClose:
        close = None

    _safe_close(_AgentNoneClose())


# --- build_agent -----------------------------------------------------------


class _FakeAgent:
    """Minimal stand-in for pydantic-ai's Agent that records its kwargs."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _install_fake_pydantic_ai(monkeypatch) -> type[_FakeAgent]:
    """Inject a fake ``pydantic_ai`` module exposing the recording Agent."""
    fake_module = types.ModuleType("pydantic_ai")
    fake_module.Agent = _FakeAgent  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pydantic_ai", fake_module)
    return _FakeAgent


def test_build_agent_assembles_minimal_kwargs(monkeypatch):
    """With only required args, build_agent forwards the documented kwargs."""
    _install_fake_pydantic_ai(monkeypatch)

    model_sentinel = object()
    http_sentinel = object()
    handle = build_agent(
        model_sentinel,
        http_sentinel,
        system_prompt="hello-prompt",
    )

    assert isinstance(handle, AgentHandle)
    inner: _FakeAgent = handle._agent  # type: ignore[assignment]
    assert isinstance(inner, _FakeAgent)
    assert inner.kwargs == {
        "model": model_sentinel,
        "system_prompt": "hello-prompt",
        "output_type": str,
        "tools": [],
        "retries": 2,
    }
    # When no name is supplied, the kwarg is omitted entirely (not None).
    assert "name" not in inner.kwargs
    # http_client is stored on the handle for later cleanup.
    assert handle._http_client is http_sentinel


def test_build_agent_passes_name_when_supplied(monkeypatch):
    """A non-None ``name`` is included in the Agent kwargs."""
    _install_fake_pydantic_ai(monkeypatch)

    handle = build_agent(
        model=object(),
        http_client=object(),
        system_prompt="p",
        name="my-agent",
    )
    inner: _FakeAgent = handle._agent  # type: ignore[assignment]
    assert inner.kwargs["name"] == "my-agent"


def test_build_agent_forwards_tools_output_type_and_retries(monkeypatch):
    """Tools, output_type and retries land verbatim in the Agent kwargs."""
    _install_fake_pydantic_ai(monkeypatch)

    def tool_a() -> None:
        return None

    def tool_b() -> None:
        return None

    class _Out:
        pass

    handle = build_agent(
        model=object(),
        http_client=object(),
        system_prompt="prompt",
        tools=[tool_a, tool_b],
        output_type=_Out,
        retries=5,
    )
    inner: _FakeAgent = handle._agent  # type: ignore[assignment]
    assert inner.kwargs["tools"] == [tool_a, tool_b]
    # build_agent normalises tools via ``list(tools or [])`` — make sure it's
    # a fresh list, not the caller's reference (caller can mutate without
    # affecting the agent).
    assert inner.kwargs["tools"] is not None
    assert inner.kwargs["output_type"] is _Out
    assert inner.kwargs["retries"] == 5


def test_build_agent_normalises_none_tools_to_empty_list(monkeypatch):
    """When tools=None is passed, the Agent still receives an empty list."""
    _install_fake_pydantic_ai(monkeypatch)

    handle = build_agent(
        model=object(),
        http_client=object(),
        system_prompt="prompt",
        tools=None,
    )
    inner: _FakeAgent = handle._agent  # type: ignore[assignment]
    assert inner.kwargs["tools"] == []
