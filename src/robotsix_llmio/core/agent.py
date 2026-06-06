"""Generic pydantic-ai Agent assembly + deterministic HTTP-client cleanup."""

from __future__ import annotations

from typing import Any

from .http import _close_async_client


class AgentHandle:
    """Wraps a pydantic-ai Agent with its httpx client so callers can
    deterministically close the client after use. Delegates attribute access
    to the underlying agent so existing call sites (and test mocks) work
    unchanged."""

    def __init__(self, agent: Any, http_client: Any) -> None:
        self._agent = agent
        self._http_client = http_client

    def close(self) -> None:
        """Close the HTTP client. Idempotent."""
        if self._http_client is not None:
            _close_async_client(self._http_client)
            self._http_client = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)


def build_agent(
    model: Any,
    http_client: Any,
    *,
    system_prompt: str,
    tools: list | None = None,
    output_type: Any = str,
    name: str | None = None,
    retries: int = 2,
) -> AgentHandle:
    """Assemble a pydantic-ai ``Agent`` from an already-configured *model* and
    wrap it (with *http_client*) in an :class:`AgentHandle`.

    Provider-agnostic: the *model* carries provider/pin/reasoning/cost; this
    function only does the generic Agent wiring. The system prompt, tools, and
    output_type are supplied verbatim by the caller (the consumer owns prompt
    composition and tool selection)."""
    from pydantic_ai import Agent

    agent_kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": system_prompt,
        "output_type": output_type,
        "tools": list(tools or []),
        "retries": retries,
    }
    if name is not None:
        agent_kwargs["name"] = name
    return AgentHandle(Agent(**agent_kwargs), http_client)
