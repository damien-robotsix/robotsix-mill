"""Surface OpenRouter's per-call USD cost on the OTel model-call span.

pydantic-ai's stock instrumentation can't price OpenRouter-prefixed
model names, so cost is dropped. OpenRouter returns the realised cost in
``response.usage.cost`` (USD) when usage-accounting is opted in; we copy
it onto the active OTel span so Langfuse's OTLP ingestor populates
``observation.totalCost`` (→ ``trace.totalCost``). gen_ai semantic-
convention attrs are also set so Langfuse classifies the span as a
*generation* (without them ``totalCost`` stays 0).

Per-ticket cost attribution is handled by a periodic sync loop in the
worker that reads Langfuse session totals (``session.id = ticket.id``)
and writes them to ``ticket.cost_usd`` — the real-time contextvar path
was removed because it leaked across concurrent tickets.

``opentelemetry`` is imported lazily inside :func:`record_openrouter_cost`
so this module is usable without the ``[tracing]`` extra (cost recording
is simply a no-op when OTel isn't installed / no span is recording).
Ported from robotsix-project.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai.models.openai import OpenAIChatModel


def _get_cost_from_response(response: Any) -> float | None:
    """Extract the USD cost from an OpenRouter completion response.

    Returns ``None`` when the response doesn't carry usage or cost info
    (e.g. a streaming-only model, or usage accounting not opted in)."""
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
    except TypeError, ValueError:
        return None


class CostInstrumentedOpenRouterModel(OpenAIChatModel):
    """OpenAIChatModel that emits OpenRouter's ``usage.cost`` on the
    OTel span so Langfuse can sum it into session totals.

    Forces OpenRouter's usage-accounting opt-in
    (``usage: {include: true}``) so the response carries ``usage.cost``.
    """

    async def _completions_create(self, *args: Any, **kwargs: Any) -> Any:
        _inject_usage_include(args, kwargs)
        response = await super()._completions_create(*args, **kwargs)
        record_openrouter_cost(response)
        return response


def _inject_usage_include(args: tuple, kwargs: dict) -> None:
    """Merge ``extra_body.usage.include = True`` onto ``model_settings``
    without trampling a caller-supplied ``extra_body`` (e.g. web plugin).
    Parent signature: ``(messages, stream, model_settings, params)``."""
    settings: Any = None
    if "model_settings" in kwargs:
        settings = kwargs["model_settings"]
    elif len(args) >= 3:
        settings = args[2]
    if settings is None:
        return

    extra_body = dict(settings.get("extra_body") or {})
    usage_opt = dict(extra_body.get("usage") or {})
    usage_opt.setdefault("include", True)
    extra_body["usage"] = usage_opt
    settings["extra_body"] = extra_body


def record_openrouter_cost(response: Any) -> None:
    """Copy ``usage.cost`` (+ tokens + gen_ai attrs) onto the current
    OTel span. No-op when there's no usage/cost, no recording span, or
    OpenTelemetry isn't installed."""
    cost = _get_cost_from_response(response)
    if cost is None:
        return

    try:
        from opentelemetry import trace as otel_trace
    except ImportError:
        return  # no [tracing] extra → nowhere to record cost

    span = otel_trace.get_current_span()
    if span is None or not span.is_recording():
        return

    usage_obj = getattr(response, "usage", None)

    span.set_attribute("gen_ai.usage.cost", cost)
    span.set_attribute("langfuse.observation.cost_details", json.dumps({"total": cost}))
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.provider.name", "openrouter")
    span.set_attribute("gen_ai.system", "openrouter")

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

        # --- OpenRouter cache & reasoning token details ---
        prompt_details = getattr(usage_obj, "prompt_tokens_details", None)
        if prompt_details is not None:
            # Support both dict and object shapes (varies by provider).
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
