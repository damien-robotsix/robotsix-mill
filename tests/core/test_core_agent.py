"""Generic pydantic-ai Agent assembly — ``AgentHandle`` delegation/cleanup
plus ``build_agent`` kwargs wiring, all exercised against in-memory seams so
no real ``pydantic_ai.Agent``, httpx client, or network is constructed."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from robotsix_llmio.core import agent as agent_module
from robotsix_llmio.core.agent import AgentHandle, build_agent

# --- §1 AgentHandle.__getattr__ delegation ---------------------------------


def test_getattr_forwards_plain_attribute():
    """A name absent on the handle but present on the wrapped agent resolves
    through ``__getattr__`` to the underlying agent's value. ``_agent`` is a
    real ``__init__`` attribute, so it is reached directly and never triggers
    the delegation path."""
    wrapped = SimpleNamespace(some_attr="value")
    handle = AgentHandle(agent=wrapped, http_client=SimpleNamespace())
    assert handle.some_attr == "value"


def test_getattr_forwards_method_with_binding():
    """Method access must forward the *bound* method so ``self`` is the
    wrapped agent, not the handle — calling through the handle returns the
    underlying method's result computed against the agent's own state."""

    class _Agent:
        def __init__(self) -> None:
            self.base = 10

        def add(self, n: int) -> int:
            return self.base + n

    wrapped = _Agent()
    handle = AgentHandle(agent=wrapped, http_client=SimpleNamespace())
    assert handle.add(5) == 15


def test_getattr_missing_name_raises_attributeerror():
    """A name absent on both the handle and the wrapped agent propagates
    ``AttributeError`` from the delegated ``getattr`` rather than being
    silently swallowed."""
    handle = AgentHandle(agent=SimpleNamespace(), http_client=SimpleNamespace())
    with pytest.raises(AttributeError):
        _ = handle.nope


# --- §2 AgentHandle.close() and idempotence --------------------------------


def test_close_calls_close_async_client_once_and_nulls_client(monkeypatch):
    """``close()`` routes the wrapped ``http_client`` through
    ``_close_async_client`` exactly once and then nulls ``_http_client``.
    ``agent.py`` does ``from .http import _close_async_client``, so patch the
    *consumer* namespace ``agent_module._close_async_client``."""
    calls: list[Any] = []
    monkeypatch.setattr(agent_module, "_close_async_client", lambda c: calls.append(c))

    client = SimpleNamespace()
    handle = AgentHandle(agent=SimpleNamespace(), http_client=client)
    handle.close()

    assert calls == [client]
    assert handle._http_client is None


def test_close_is_idempotent(monkeypatch):
    """A second ``close()`` is a no-op: the ``if self._http_client is not
    None`` guard short-circuits once the client has been nulled, so
    ``_close_async_client`` is not invoked again."""
    calls: list[Any] = []
    monkeypatch.setattr(agent_module, "_close_async_client", lambda c: calls.append(c))

    handle = AgentHandle(agent=SimpleNamespace(), http_client=SimpleNamespace())
    handle.close()
    handle.close()

    assert len(calls) == 1


def test_close_with_none_http_client_is_noop(monkeypatch):
    """A handle constructed with ``http_client=None`` makes ``close()`` a
    safe no-op — the guard never reaches ``_close_async_client``."""
    calls: list[Any] = []
    monkeypatch.setattr(agent_module, "_close_async_client", lambda c: calls.append(c))

    handle = AgentHandle(agent=SimpleNamespace(), http_client=None)
    handle.close()

    assert calls == []


# --- §3 build_agent() kwargs assembly --------------------------------------


class _FakeAgent:
    """Stand-in for ``pydantic_ai.Agent`` that captures its construction
    kwargs instead of building a real agent."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _patch_agent(monkeypatch) -> None:
    """Intercept the lazy ``from pydantic_ai import Agent`` inside
    ``build_agent`` by patching the attribute on the ``pydantic_ai`` module."""
    monkeypatch.setattr("pydantic_ai.Agent", _FakeAgent)


def test_build_agent_minimal_call_wraps_sentinel_and_defaults(monkeypatch):
    """A minimal call wraps the constructed (fake) agent and the passed
    ``http_client`` in an ``AgentHandle`` (reached via the real ``_agent`` /
    ``_http_client`` attributes). The always-present kwargs are forwarded;
    ``tools`` defaults to a fresh empty list and ``name`` is absent."""
    _patch_agent(monkeypatch)
    model = SimpleNamespace()
    http_client = SimpleNamespace()

    handle = build_agent(model, http_client, system_prompt="hi")

    assert isinstance(handle, AgentHandle)
    assert isinstance(handle._agent, _FakeAgent)
    assert handle._http_client is http_client

    kwargs = handle._agent.kwargs
    assert kwargs["model"] is model
    assert kwargs["system_prompt"] == "hi"
    assert kwargs["output_type"] is str
    assert kwargs["tools"] == []
    assert kwargs["retries"] == 2
    assert "name" not in kwargs


def test_build_agent_tools_none_yields_fresh_list(monkeypatch):
    """With ``tools=None`` the assembled ``tools`` kwarg is a brand-new list
    (``list(tools or [])``), not a shared/mutable default."""
    _patch_agent(monkeypatch)
    handle_a = build_agent(SimpleNamespace(), SimpleNamespace(), system_prompt="a")
    handle_b = build_agent(SimpleNamespace(), SimpleNamespace(), system_prompt="b")

    tools_a = handle_a._agent.kwargs["tools"]
    tools_b = handle_b._agent.kwargs["tools"]
    assert tools_a == [] and tools_b == []
    assert tools_a is not tools_b


def test_build_agent_full_call_forwards_all_optionals(monkeypatch):
    """A full call with custom ``tools``, ``output_type``, ``name`` and
    ``retries`` forwards each verbatim. ``tools`` is copied into a fresh list
    equal to the input, and ``name`` is present when supplied."""
    _patch_agent(monkeypatch)
    model = SimpleNamespace()
    http_client = SimpleNamespace()
    tools = [object(), object()]

    handle = build_agent(
        model,
        http_client,
        system_prompt="prompt",
        tools=tools,
        output_type=int,
        name="x",
        retries=5,
    )

    kwargs = handle._agent.kwargs
    assert kwargs["model"] is model
    assert kwargs["system_prompt"] == "prompt"
    assert kwargs["output_type"] is int
    assert kwargs["retries"] == 5
    assert kwargs["name"] == "x"
    assert kwargs["tools"] == tools
    assert kwargs["tools"] is not tools
