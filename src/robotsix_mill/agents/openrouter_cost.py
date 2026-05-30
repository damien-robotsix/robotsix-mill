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

# OpenRouter load-balances each model across ~14 upstream providers, and
# prompt caches are per-provider — so routing that bounces between providers
# turns the ~80k-token prefix into a cold miss (~13x the cost of a cache
# read). We pin DeepSeek calls to DeepSeek's own endpoint to keep the cache
# warm across the many tool-call turns of a stage (see _inject_provider_pin).
#
# Caveat under investigation: pinned to DeepSeek first-party, deepseek-v4-pro
# can intermittently 400 with "reasoning_content in the thinking mode must be
# passed back" (an earlier pin-only attempt blocked tickets — PR #503). It was
# NOT reproducible in isolation, so rather than ship an unproven field-rename
# fix we classify that 400 as transient (agents/retry.py) and retry it, which
# also tells us whether the error is intermittent. Revisit if retries exhaust.
_PINNED_PROVIDER = "DeepSeek"
_PIN_MODEL_PREFIX = "deepseek/"


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

    For ``deepseek/*`` models it pins the upstream provider to DeepSeek so
    the prompt cache stays warm across turns (see module note).
    """

    async def _completions_create(self, *args: Any, **kwargs: Any) -> Any:
        _inject_usage_include(args, kwargs)
        _inject_provider_pin(args, kwargs, str(getattr(self, "model_name", "") or ""))
        response = await super()._completions_create(*args, **kwargs)
        record_openrouter_cost(response)
        return response


def _inject_provider_pin(args: tuple, kwargs: dict, model_name: str) -> None:
    """Pin ``deepseek/*`` calls to the DeepSeek upstream provider so the
    prompt cache stays warm across turns (see module note). Hard pin (no
    fallbacks). No-op for non-DeepSeek models and when a caller already set
    ``provider`` (don't trample an explicit override). MUST be paired with
    the ``reasoning_content`` profile override in ``__init__``."""
    if not model_name.startswith(_PIN_MODEL_PREFIX):
        return
    settings = _resolve_model_settings(args, kwargs)
    if settings is None:
        return
    extra_body = dict(settings.get("extra_body") or {})
    if "provider" in extra_body:
        return  # caller set an explicit routing preference — respect it
    extra_body["provider"] = {"only": [_PINNED_PROVIDER], "allow_fallbacks": False}
    settings["extra_body"] = extra_body


def _resolve_model_settings(args: tuple, kwargs: dict) -> Any:
    """Return the mutable ``model_settings`` dict from the parent call.
    Parent signature: ``(messages, stream, model_settings, params)``."""
    if "model_settings" in kwargs:
        return kwargs["model_settings"]
    if len(args) >= 3:
        return args[2]
    return None


def _inject_usage_include(args: tuple, kwargs: dict) -> None:
    """Merge ``extra_body.usage.include = True`` onto ``model_settings``
    without trampling a caller-supplied ``extra_body`` (e.g. web plugin)."""
    settings = _resolve_model_settings(args, kwargs)
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
