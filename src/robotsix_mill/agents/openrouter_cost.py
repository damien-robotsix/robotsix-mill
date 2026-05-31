"""Compatibility shim — LLM-I/O model now lives in the robotsix-llmio library.

The OpenRouter cost-instrumented model, the DeepSeek provider pin, the reasoning
round-trip, and cost recording were extracted into the standalone
``robotsix-llmio`` package (layers ``openrouter`` + ``openrouter_deepseek``).
This module re-exports those symbols under their historical names so the ~6
modules that construct a model directly (explore, web_research, consult_expert,
trace_inspector, cross_trace_analyzer, web_knowledge) and the existing tests
keep working unchanged.

The one behaviour preserved here is the *interim* substring-based tiering for
direct constructors: a model whose name contains ``flash`` runs with reasoning
disabled, otherwise xhigh. Once call sites pass a ``Tier`` explicitly this shim
(and this file) can be deleted.
"""

from __future__ import annotations

from typing import Any

from robotsix_llmio.openrouter.model import (
    _get_cost_from_response,
    _inject_usage_include,
    _resolve_model_settings,
    record_openrouter_cost,
)
from robotsix_llmio.openrouter_deepseek.model import (
    _PIN_MODEL_PREFIX,
    _PINNED_PROVIDER,
    OpenRouterDeepseekModel,
)

# NOTE: the reasoning_details echo/strip round-trip (_extract_reasoning_details,
# _REASONING_DETAILS_KEY, _EMPTY_REASONING) was removed from robotsix-llmio —
# OpenRouter no longer raises the DeepSeek thinking-mode 400 when reasoning is
# stripped from a tool-call turn, so the backport is obsolete. These names are
# no longer importable or re-exported.
__all__ = [
    "CostInstrumentedOpenRouterModel",
    "record_openrouter_cost",
    "_get_cost_from_response",
    "_inject_usage_include",
    "_inject_provider_pin",
    "_PINNED_PROVIDER",
    "_PIN_MODEL_PREFIX",
]

_FLASH_MARKER = "flash"


def _inject_provider_pin(args: tuple, kwargs: dict, model_name: str) -> None:
    """Interim substring-based DeepSeek pin (compat with the pre-extraction
    behaviour). The library applies the pin per-instance instead; this function
    remains for direct callers/tests that pass a model name."""
    if not model_name.startswith(_PIN_MODEL_PREFIX):
        return
    settings = _resolve_model_settings(args, kwargs)
    if settings is None:
        return
    extra_body = dict(settings.get("extra_body") or {})
    if "provider" in extra_body:
        return
    extra_body["provider"] = {"only": [_PINNED_PROVIDER], "allow_fallbacks": False}
    if "reasoning" not in extra_body:
        if _FLASH_MARKER in model_name:
            extra_body["reasoning"] = {"enabled": False}
        else:
            extra_body["reasoning"] = {"effort": "xhigh"}
    settings["extra_body"] = extra_body


class CostInstrumentedOpenRouterModel(OpenRouterDeepseekModel):
    """DeepSeek-on-OpenRouter model with the interim substring tiering applied
    at construction, so direct constructors keep their historical behaviour
    (flash → reasoning disabled; else → xhigh)."""

    def __init__(self, model_name: str, **kwargs: Any) -> None:
        super().__init__(model_name, **kwargs)
        if _FLASH_MARKER in str(model_name):
            self.reasoning_setting = {"enabled": False}
        else:
            self.reasoning_setting = {"effort": "xhigh"}
