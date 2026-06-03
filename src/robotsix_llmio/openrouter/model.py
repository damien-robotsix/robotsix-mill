"""OpenRouter transport model — usage accounting + cost recording only.

Model-family agnostic: this layer knows how to talk to OpenRouter (opt into
``usage.include`` and read ``usage.cost``) but applies no provider pin and no
reasoning policy. Those quirks live in derived layers (e.g.
``openrouter_deepseek``).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai.models.openai import OpenAIChatModel

from robotsix_llmio.core.tracing import get_recording_span

PROVIDER_NAME: str = "openrouter"


def _resolve_model_settings(args: tuple, kwargs: dict) -> Any:
    """Return the mutable ``model_settings`` dict from the parent call.
    Parent signature: ``(messages, stream, model_settings, params)``."""
    if "model_settings" in kwargs:
        return kwargs["model_settings"]
    if len(args) >= 3:
        return args[2]
    return None


def _inject_usage_include(args: tuple, kwargs: dict) -> None:
    """Merge ``extra_body.usage.include = True`` onto ``model_settings`` without
    trampling a caller-supplied ``extra_body``."""
    settings = _resolve_model_settings(args, kwargs)
    if settings is None:
        return
    extra_body = dict(settings.get("extra_body") or {})
    usage_opt = dict(extra_body.get("usage") or {})
    usage_opt.setdefault("include", True)
    extra_body["usage"] = usage_opt
    settings["extra_body"] = extra_body


def _get_cost_from_response(response: Any) -> float | None:
    """Extract the USD cost from an OpenRouter completion response, or ``None``
    when the response carries no usage/cost info."""
    usage_obj = getattr(response, "usage", None)
    if usage_obj is None:
        return None
    extras = getattr(usage_obj, "model_extra", None)
    raw_cost: Any = None
    if isinstance(extras, dict):
        raw_cost = extras.get("cost")
    if raw_cost is None:
        raw_cost = getattr(usage_obj, "cost", None)
    if raw_cost is None:
        return None
    try:
        return float(raw_cost)
    except (TypeError, ValueError):
        return None


def record_openrouter_cost(response: Any) -> None:
    """Copy ``usage.cost`` (+ tokens + cache details + gen_ai attrs) onto the
    current OTel span. No-op without a cost, a recording span, or OpenTelemetry.
    """
    cost = _get_cost_from_response(response)
    if cost is None:
        return
    span = get_recording_span()
    if span is None:
        return

    usage_obj = getattr(response, "usage", None)
    span.set_attribute("gen_ai.usage.cost", cost)
    span.set_attribute("langfuse.observation.cost_details", json.dumps({"total": cost}))
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.provider.name", PROVIDER_NAME)
    span.set_attribute("gen_ai.system", PROVIDER_NAME)
    # Provider tag Langfuse indexes onto the observation's metadata, so a
    # consumer can sum logged cost PER PROVIDER (cost reconciliation filters
    # the logged side to "openrouter" to match an OpenRouter key's scope).
    span.set_attribute("langfuse.observation.metadata.provider", PROVIDER_NAME)

    model = getattr(response, "model", None)
    if model:
        span.set_attribute("gen_ai.request.model", model)
    if usage_obj is not None:
        prompt_tokens = getattr(usage_obj, "prompt_tokens", None)
        if prompt_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
        completion_tokens = getattr(usage_obj, "completion_tokens", None)
        if completion_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
        prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
        if prompt_details is not None:
            if isinstance(prompt_details, dict):
                cached = prompt_details.get("cached_tokens")
                cache_creation = prompt_details.get("cache_creation_input_tokens")
            else:
                cached = getattr(prompt_details, "cached_tokens", None)
                cache_creation = getattr(
                    prompt_details, "cache_creation_input_tokens", None
                )
            if cached is not None:
                span.set_attribute("gen_ai.usage.cache_read_input_tokens", cached)
            if cache_creation is not None:
                span.set_attribute(
                    "gen_ai.usage.cache_creation_input_tokens", cache_creation
                )
        completion_details = getattr(usage_obj, "completion_tokens_details", None)
        if completion_details is not None:
            if isinstance(completion_details, dict):
                reasoning = completion_details.get("reasoning_tokens")
            else:
                reasoning = getattr(completion_details, "reasoning_tokens", None)
            if reasoning is not None:
                span.set_attribute("gen_ai.usage.reasoning_tokens", reasoning)


class OpenRouterModel(OpenAIChatModel):
    """``OpenAIChatModel`` that opts into OpenRouter usage accounting and emits
    ``usage.cost`` onto the active OTel span. No pin, no reasoning policy."""

    async def _completions_create(self, *args: Any, **kwargs: Any) -> Any:
        _inject_usage_include(args, kwargs)
        response = await super()._completions_create(*args, **kwargs)
        record_openrouter_cost(response)
        return response
