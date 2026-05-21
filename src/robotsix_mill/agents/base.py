"""pydantic-ai agent factory over OpenRouter.

``pydantic_ai`` is imported lazily inside :func:`build_agent` so the
core imports without the heavy LLM stack and runs offline. The main
agent's model is ALWAYS the plain (non-``:online``) model — web search
would otherwise bill a per-request surcharge on this expensive model.
``web=True`` instead exposes a single ``web_research`` tool that
delegates to a cheap, bounded sub-agent (see :mod:`.web_research`) and
returns only its conclusion. Skills are always injected into the
prompt. ``_model_name`` and skill assembly are factored out so they're
unit-testable without a key or pydantic_ai.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

from ..config import Settings
from .report_issue import make_report_issue_tool
from .skills import load_skills
from .web_research import make_web_research_tool


def _close_async_client(client: "httpx.AsyncClient") -> None:
    """Close an httpx.AsyncClient from outside its original event loop.

    Creates a temporary event loop to run aclose(), catching any errors
    so cleanup never raises in a finally/del context."""
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(client.aclose())
        loop.close()
    except Exception:
        pass


def _safe_close(agent: Any) -> None:
    """Close an agent's HTTP client if it has a close method.

    Safe to call on any object — silently no-ops if the object lacks
    a ``close`` method or if closing raises."""
    close_fn = getattr(agent, "close", None)
    if close_fn is not None:
        try:
            close_fn()
        except Exception:
            pass


def timeout_http_client(settings: Settings):
    """A fresh httpx.AsyncClient with a hard per-request timeout, so a
    hung/glacial provider connection raises instead of blocking the
    worker forever. Pass to OpenRouterProvider(http_client=...)."""
    import httpx

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.model_request_timeout, connect=15.0)
    )
    weakref.finalize(client, _close_async_client, client)
    return client


class AgentHandle:
    """Wraps a pydantic-ai Agent with its httpx client so callers can
    deterministically close the client after use.

    Delegates attribute access to the underlying agent so existing
    code (including test mocks) works unchanged."""

    def __init__(self, agent: Any, http_client: Any) -> None:
        self._agent = agent
        self._http_client = http_client

    def close(self) -> None:
        """Close the HTTP client. Idempotent; safe to call multiple times."""
        if self._http_client is not None:
            _close_async_client(self._http_client)
            self._http_client = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)


def _model_name(settings: Settings) -> str:
    # No "openrouter:" prefix — the provider is set explicitly so we can
    # use the cost-instrumented model subclass. The main agent NEVER
    # gets ":online": web search lives only in the cheap web_research
    # sub-agent, so the pricey model isn't surcharged on every request.
    return settings.model


def _compose_prompt(settings: Settings, system_prompt: str) -> str:
    return system_prompt + load_skills(settings.skills_dir)


def build_agent(
    settings: Settings,
    *,
    system_prompt: str,
    output_type: Any = str,
    tools: list | None = None,
    web: bool = False,
    report_issue: bool = True,
    model_name: str | None = None,
    name: str | None = None,
):
    """Construct a pydantic-ai Agent on an OpenRouter model. Each agent
    role passes its own ``model_name`` (see Settings per-agent models);
    falls back to the coordinator ``model``. Raises if no key.

    Set ``report_issue=False`` for agents that already emit draft
    tickets through their structured output (audit, retrospect).

    Note: for a structured ``output_type`` on a model whose provider
    rejects forced ``tool_choice``, wrap it in ``PromptedOutput`` at
    the call site (the default ``ToolOutput`` mode 404s there)."""
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    # lazy: keeps core import-light and the test suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from .openrouter_cost import CostInstrumentedOpenRouterModel

    http_client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        model_name or _model_name(settings),
        provider=OpenRouterProvider(
            api_key=settings.openrouter_api_key,
            http_client=http_client,
        ),
    )

    all_tools = list(tools or [])
    if report_issue:
        # Every agent can self-report a blocking/degrading issue (missing
        # tool, error, workflow gap, missing input) as a draft ticket.
        # Dedup-guarded so a looping agent can't spam identical tickets.
        all_tools.append(make_report_issue_tool(settings))
    if web:
        # Not ":online", not web_fetch on the main agent — a cheap
        # sub-agent does the searching and hands back only a conclusion.
        all_tools.append(make_web_research_tool(settings))

    agent_kwargs: dict[str, Any] = dict(
        model=model,
        system_prompt=_compose_prompt(settings, system_prompt),
        output_type=output_type,
        tools=all_tools,
    )
    if name is not None:
        agent_kwargs["name"] = name
    agent = Agent(**agent_kwargs)
    return AgentHandle(agent, http_client)
