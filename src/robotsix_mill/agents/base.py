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

from typing import Any

from ..config import Settings
from .skills import load_skills
from .web_research import make_web_research_tool


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
    model_name: str | None = None,
):
    """Construct a pydantic-ai Agent bound to an OpenRouter model.
    Raises if no OpenRouter key is configured.

    ``model_name`` overrides the default cheap driver model — required
    for agents with a structured ``output_type``, since pydantic-ai
    forces that via ``tool_choice`` and the cheap driver model has no
    OpenRouter endpoint supporting it (404). Such agents pass the
    strong ``deep_model`` here."""
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    # lazy: keeps core import-light and the test suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from .openrouter_cost import CostInstrumentedOpenRouterModel

    model = CostInstrumentedOpenRouterModel(
        model_name or _model_name(settings),
        provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
    )

    all_tools = list(tools or [])
    if web:
        # Not ":online", not web_fetch on the main agent — a cheap
        # sub-agent does the searching and hands back only a conclusion.
        all_tools.append(make_web_research_tool(settings))

    return Agent(
        model=model,
        system_prompt=_compose_prompt(settings, system_prompt),
        output_type=output_type,
        tools=all_tools,
    )
