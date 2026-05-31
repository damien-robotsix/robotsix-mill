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
# ⚠️ TEMPORARY WORKAROUND — remove once pydantic-ai natively round-trips
# reasoning_details (upstream issue pydantic/pydantic-ai#2701; PR #2823 only
# echoes the reasoning *text*, which is insufficient — verified still missing
# in 1.104). The survey agent's memory tracks the upstream status and will
# surface a draft when it lands; at that point delete _extract_reasoning_details,
# and the _process_response/_map_model_response overrides here.
#
# Why it's needed: pinned to DeepSeek first-party, deepseek models run in
# thinking mode and DeepSeek requires the prior turn's reasoning echoed back
# *matching what it generated* — especially on tool-call turns — or it 400s
# ("reasoning_content in the thinking mode must be passed back"). pydantic-ai
# (through 1.104) captures only the reasoning *text* (ThinkingPart) and
# DISCARDS the structured ``reasoning_details`` array, so the bare-text echo
# doesn't match → 400 (proven deterministic across review/implement/document).
# Fix: capture the raw ``reasoning_details`` into ``ModelResponse.provider_details``
# on the way in (_process_response) and re-emit that exact array on the
# assistant message on the way out (_map_model_response). provider_details
# survives the conversation_state JSON round-trip, so multi-turn histories
# stay valid. Without the pin this never triggers; the pin + this fix are paired.
_PINNED_PROVIDER = "DeepSeek"
_PIN_MODEL_PREFIX = "deepseek/"
_REASONING_DETAILS_KEY = "reasoning_details"
# Flash-tier models run with reasoning DISABLED (verdict/generation work, no
# deep CoT) — keeps them clear of the DeepSeek thinking-mode round-trip 400.
_FLASH_MARKER = "flash"


def _extract_reasoning_details(response: Any) -> Any:
    """Pull the raw ``reasoning_details`` array off an OpenRouter chat
    completion response message, or ``None``. Handles the str-response
    branch (no message) and the OpenAI SDK's ``model_extra`` stash for
    fields outside the typed schema."""
    choices = getattr(response, "choices", None)
    if not choices:
        return None
    msg = getattr(choices[0], "message", None)
    if msg is None:
        return None
    rd = getattr(msg, "reasoning_details", None)
    if rd is None:
        extra = getattr(msg, "model_extra", None)
        if isinstance(extra, dict):
            rd = extra.get(_REASONING_DETAILS_KEY)
    return rd if rd is not None else None


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
    the prompt cache stays warm across turns, and round-trips DeepSeek's
    structured ``reasoning_details`` across turns so thinking mode accepts
    follow-up/tool-call turns (backport of pydantic-ai #2701, see module note).
    """

    def _process_response(self, response: Any) -> Any:
        result = super()._process_response(response)
        rd = _extract_reasoning_details(response)
        if rd is not None:
            pd = dict(result.provider_details or {})
            pd[_REASONING_DETAILS_KEY] = rd
            result.provider_details = pd
        return result

    def _map_model_response(self, message: Any) -> Any:
        param = super()._map_model_response(message)
        if not (isinstance(param, dict) and param.get("role") == "assistant"):
            return param
        # Flash tier runs with reasoning DISABLED (see _inject_provider_pin),
        # so the model emits no reasoning_content. Strip ANY reasoning fields
        # from every echoed turn — including synthetic preseed tool-call turns
        # — so the request is consistently reasoning-free and DeepSeek's
        # thinking-mode mix-400 cannot fire. (Pro tier below keeps the
        # xhigh + reasoning_details round-trip.)
        if _FLASH_MARKER in str(getattr(self, "model_name", "") or ""):
            param.pop("reasoning", None)
            param.pop("reasoning_content", None)
            param.pop(_REASONING_DETAILS_KEY, None)
            return param
        # DeepSeek thinking-mode rule (api-docs.deepseek.com/guides/thinking_mode):
        # reasoning must be echoed back ONLY on assistant turns that performed
        # a tool call; for non-tool-call turns the prior CoT is NOT concatenated
        # and must be OMITTED — including it makes the echoed sequence mismatch
        # what the model generated and triggers the 400.
        if param.get("tool_calls"):
            rd = (message.provider_details or {}).get(_REASONING_DETAILS_KEY)
            if rd:
                # Send the exact reasoning_details array; drop the lossy bare
                # fields so the two can't disagree.
                param.pop("reasoning", None)
                param.pop("reasoning_content", None)
                param[_REASONING_DETAILS_KEY] = rd
            else:
                # rd missing on a tool-call turn (model emitted no reasoning
                # that turn, or extraction failed). OMITTING it makes the
                # echoed sequence a MIX — some tool-call turns carry
                # reasoning_details, some don't — which is exactly what
                # DeepSeek's thinking-mode validation rejects with the
                # deterministic 400. Instead send the field PRESENT but
                # EMPTY, so EVERY tool-call turn consistently carries a
                # reasoning_details entry. Verified flash accepts an
                # empty-text entry (status 200, no malformed-rejection);
                # this converts the failing "mix" into the passing
                # "all-present" sequence. Matches the real entry shape
                # ({type, text, format}) with empty text.
                param.pop("reasoning", None)
                param.pop("reasoning_content", None)
                param[_REASONING_DETAILS_KEY] = [
                    {"type": "reasoning.text", "text": "", "format": "unknown"}
                ]
        else:
            # No tool call → omit reasoning entirely.
            param.pop("reasoning", None)
            param.pop("reasoning_content", None)
            param.pop(_REASONING_DETAILS_KEY, None)
        return param

    async def _completions_create(self, *args: Any, **kwargs: Any) -> Any:
        _inject_usage_include(args, kwargs)
        _inject_provider_pin(args, kwargs, str(getattr(self, "model_name", "") or ""))
        response = await super()._completions_create(*args, **kwargs)
        record_openrouter_cost(response)
        return response


def _inject_provider_pin(args: tuple, kwargs: dict, model_name: str) -> None:
    """Pin ``deepseek/*`` calls to the DeepSeek upstream provider (keeps the
    prompt cache warm) AND force a CONSISTENT reasoning mode so DeepSeek's
    thinking-mode round-trip validation doesn't break on a mixed sequence.

    The 400 ("reasoning_content must be passed back") is triggered by an
    *inconsistent* mix of reasoning / no-reasoning assistant turns — proven
    by direct test: all-reasoning and all-no-reasoning both pass, the mix
    fails. We force reasoning to ``effort: xhigh`` for ALL deepseek models
    (pro AND flash) because ``enabled: false`` does not reliably suppress
    thinking on DeepSeek V4 Flash, so disabling it produced the same 400.
    Consistent reasoning on every turn, paired with the reasoning_details
    round-trip in this class, eliminates the deterministic error.

    No-op for non-DeepSeek models and when a caller already pinned
    ``provider`` (don't trample an explicit override)."""
    if not model_name.startswith(_PIN_MODEL_PREFIX):
        return
    settings = _resolve_model_settings(args, kwargs)
    if settings is None:
        return
    extra_body = dict(settings.get("extra_body") or {})
    if "provider" in extra_body:
        return  # caller set an explicit routing preference — respect it
    extra_body["provider"] = {"only": [_PINNED_PROVIDER], "allow_fallbacks": False}
    if "reasoning" not in extra_body:
        # Tiered reasoning. PRO does deep work → keep xhigh. FLASH does
        # verdict/generation work (review, document, summaries) that needs no
        # chain-of-thought → DISABLE reasoning. Disabling is verified to make
        # DeepSeek emit no reasoning_content at all, so the thinking-mode
        # round-trip mix-400 cannot occur on flash. (Paired with the flash
        # short-circuit in _map_model_response, which strips any reasoning
        # echo so the request stays consistently reasoning-free.)
        if _FLASH_MARKER in model_name:
            extra_body["reasoning"] = {"enabled": False}
        else:
            extra_body["reasoning"] = {"effort": "xhigh"}
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
