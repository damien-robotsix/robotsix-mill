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

from ..config import Settings, get_secrets
from .report_issue import make_report_issue_tool
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


def build_agent_from_definition(
    settings: Settings,
    definition: "AgentDefinition",
    *,
    tools: list | None = None,
    **overrides,
) -> AgentHandle:
    """Build an agent from an :class:`AgentDefinition`, bridging the YAML
    loader and the agent runtime.

    Any keyword in ``**overrides`` that matches a :func:`build_agent`
    parameter name (``system_prompt``, ``model_name``, ``output_type``,
    ``web``, ``report_issue``, ``retries``, ``name``) replaces the value
    extracted from *definition*.
    """
    import importlib

    from pydantic_ai import PromptedOutput

    # Resolve output_type
    if definition.output_type and definition.output_type.strip():
        if not definition.module or not definition.module.strip():
            raise ValueError(
                f"Agent definition '{definition.name}' specifies "
                f"output_type='{definition.output_type}' but module is None"
            )
        module = importlib.import_module(
            f"robotsix_mill.agents.{definition.module}"
        )
        output_cls = getattr(module, definition.output_type)
        resolved_output_type: Any = PromptedOutput(output_cls)
    else:
        resolved_output_type = str

    kwargs: dict[str, Any] = dict(
        name=definition.name,
        system_prompt=definition.system_prompt,
        model_name=definition.model,
        web=definition.web,
        report_issue=definition.report_issue,
        read_ticket=definition.read_ticket,
        reply_to_thread=definition.reply_to_thread,
        close_thread=definition.close_thread,
        ask_user=definition.ask_user,
        retries=definition.retries,
        output_type=resolved_output_type,
        skills=definition.skills,
    )
    kwargs.update(overrides)
    kwargs["tools"] = tools

    return build_agent(settings, **kwargs)


def _model_name(settings: Settings) -> str:
    # No "openrouter:" prefix — the provider is set explicitly so we can
    # use the cost-instrumented model subclass. The main agent NEVER
    # gets ":online": web search lives only in the cheap web_research
    # sub-agent, so the pricey model isn't surcharged on every request.
    return settings.model


def compose_prompt(
    settings: Settings,
    system_prompt: str,
    tool_names: set[str] | None = None,
    skills: list[str] | None = None,
) -> str:
    from .tool_registry import ToolRegistry

    prompt = system_prompt

    if skills:
        import logging
        import re

        logger = logging.getLogger(__name__)
        skill_sections: list[str] = []

        for name in skills:
            skill_path = settings.skills_dir / name / "SKILL.md"
            try:
                raw = skill_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                logger.warning("Skill file not found: %s", skill_path)
                continue

            # Strip YAML frontmatter (--- ... ---)
            body = re.sub(
                r"^---\n.*?\n---\n", "", raw, count=1, flags=re.DOTALL
            ).strip()

            if body:
                skill_sections.append(body)

        if skill_sections:
            prompt += "\n\n## Skills\n\n" + "\n\n".join(skill_sections)

    prompt += "\n\n" + ToolRegistry.describe_for_prompt(
        tool_names=tool_names
    )
    return prompt


def build_agent(
    settings: Settings,
    *,
    system_prompt: str,
    output_type: Any = str,
    tools: list | None = None,
    web: bool = False,
    report_issue: bool = True,
    read_ticket: bool = False,
    reply_to_thread: bool = True,
    close_thread: bool = True,
    ask_user: bool = True,
    model_name: str | None = None,
    name: str | None = None,
    retries: int = 2,
    skills: list[str] | None = None,
):
    """Construct a pydantic-ai Agent on an OpenRouter model. Each agent
    role passes its own ``model_name`` (see Settings per-agent models);
    falls back to the coordinator ``model``. Raises if no key.

    Set ``report_issue=False`` for agents that already emit draft
    tickets through their structured output (audit, retrospect).

    Note: for a structured ``output_type`` on a model whose provider
    rejects forced ``tool_choice``, wrap it in ``PromptedOutput`` at
    the call site (the default ``ToolOutput`` mode 404s there)."""
    if not get_secrets().openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    # lazy: keeps core import-light and the test suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from .openrouter_cost import CostInstrumentedOpenRouterModel

    http_client = timeout_http_client(settings)
    model = CostInstrumentedOpenRouterModel(
        model_name or _model_name(settings),
        provider=OpenRouterProvider(
            api_key=get_secrets().openrouter_api_key,
            http_client=http_client,
        ),
    )

    all_tools = list(tools or [])
    if report_issue:
        # Every agent can self-report a blocking/degrading issue (missing
        # tool, error, workflow gap, missing input) as a draft ticket.
        # Dedup-guarded so a looping agent can't spam identical tickets.
        all_tools.append(make_report_issue_tool(settings, agent_name=name))
    if read_ticket:
        # Read-only tool so periodic agents can fetch full context of a
        # past proposal when the one-line summary in <recent_proposals>
        # isn't enough. Only injected when explicitly requested.
        from .read_ticket import make_read_ticket_tool

        all_tools.append(make_read_ticket_tool(settings))
    if reply_to_thread:
        # Tool so agents can reply to a comment thread on the current
        # ticket, enabling real conversation with humans.
        from .reply_thread import make_reply_to_thread_tool

        all_tools.append(make_reply_to_thread_tool(settings, agent_name=name))
    if close_thread:
        # Tool so agents can close a comment thread on the current
        # ticket after addressing review feedback.
        from .close_thread import make_close_thread_tool

        all_tools.append(make_close_thread_tool(settings, agent_name=name))
    if ask_user:
        from .ask_user import make_ask_user_tool

        all_tools.append(make_ask_user_tool(settings, agent_name=name))
    if web:
        # Not ":online", not web_fetch on the main agent — a cheap
        # sub-agent does the searching and hands back only a conclusion.
        all_tools.append(make_web_research_tool(settings))

    tool_names = {t.__name__ for t in all_tools}

    agent_kwargs: dict[str, Any] = dict(
        model=model,
        system_prompt=compose_prompt(
            settings, system_prompt, tool_names=tool_names, skills=skills
        ),
        output_type=output_type,
        tools=all_tools,
        retries=retries,
    )
    if name is not None:
        agent_kwargs["name"] = name
    agent = Agent(**agent_kwargs)
    return AgentHandle(agent, http_client)
