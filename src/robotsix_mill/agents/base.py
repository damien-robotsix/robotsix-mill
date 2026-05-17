"""pydantic-ai agent factory over OpenRouter.

``pydantic_ai`` is imported lazily inside :func:`build_agent` so that the
core (store, supervisor, tests) imports without the heavy LLM stack and
runs offline. Stages call ``build_agent(...)`` only when they actually
need a model.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings


def build_agent(
    settings: Settings,
    *,
    system_prompt: str,
    output_type: Any = str,
    tools: list | None = None,
):
    """Construct a pydantic-ai Agent bound to the configured OpenRouter
    model. Raises if no OpenRouter key is configured."""
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")

    from pydantic_ai import Agent  # lazy: keeps core import-light

    # pydantic-ai's OpenRouter provider reads OPENROUTER_API_KEY from the
    # environment; config.py has already loaded it from .env.
    return Agent(
        f"openrouter:{settings.model}",
        system_prompt=system_prompt,
        output_type=output_type,
        tools=tools or [],
    )
