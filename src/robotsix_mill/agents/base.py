"""pydantic-ai agent factory over OpenRouter.

``pydantic_ai`` is imported lazily inside :func:`build_agent` so the
core imports without the heavy LLM stack and runs offline. ``web=True``
enables OpenRouter's server-side web search (``:online`` suffix) and adds
the ``web_fetch`` tool; skills are always injected into the prompt.
``_model_id`` and skill assembly are factored out so they're unit-
testable without a key or pydantic_ai.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from .skills import load_skills
from .web_tools import make_web_fetch


def _model_name(settings: Settings, web: bool) -> str:
    # No "openrouter:" prefix — the provider is set explicitly so we can
    # use the cost-instrumented model subclass. ":online" = OpenRouter
    # server-side web search.
    online = ":online" if (web and settings.web_search) else ""
    return f"{settings.model}{online}"


def _compose_prompt(settings: Settings, system_prompt: str) -> str:
    return system_prompt + load_skills(settings.skills_dir)


def build_agent(
    settings: Settings,
    *,
    system_prompt: str,
    output_type: Any = str,
    tools: list | None = None,
    web: bool = False,
):
    """Construct a pydantic-ai Agent bound to the configured OpenRouter
    model. Raises if no OpenRouter key is configured."""
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    # lazy: keeps core import-light and the test suite hermetic
    from pydantic_ai import Agent
    from pydantic_ai.providers.openrouter import OpenRouterProvider

    from .openrouter_cost import CostInstrumentedOpenRouterModel

    model = CostInstrumentedOpenRouterModel(
        _model_name(settings, web),
        provider=OpenRouterProvider(api_key=settings.openrouter_api_key),
    )

    all_tools = list(tools or [])
    if web:
        all_tools.append(make_web_fetch(settings))

    return Agent(
        model=model,
        system_prompt=_compose_prompt(settings, system_prompt),
        output_type=output_type,
        tools=all_tools,
    )
