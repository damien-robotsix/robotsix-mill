"""DeepSeek-on-OpenRouter model — provider pin + per-tier reasoning policy.

Extends the OpenRouter transport model with the two DeepSeek quirks that are
actually needed:
- pin the upstream provider to DeepSeek (warms the per-provider prompt cache and
  keeps routing deterministic);
- inject a per-tier reasoning policy into the request (set by the provider:
  ``{"effort": "xhigh"}`` for the capable tier, ``{"enabled": False}`` for the
  cheap tier).

It deliberately does **not** remap reasoning between turns. An earlier
``reasoning_details`` echo/strip (mill PRs #523/#529/#531) guarded against a
DeepSeek thinking-mode 400 ("reasoning_content must be passed back"), but that
400 is not reproducible through OpenRouter today: pydantic-ai round-trips
reasoning natively (``openai_chat_send_back_thinking_parts``), and OpenRouter
does not raise the 400 even when reasoning is stripped from a tool-call turn —
verified live in ``tests/test_openrouter_deepseek_live.py``. So the remap was
removed to keep this layer minimal.
"""

from __future__ import annotations

from typing import Any

from ..openrouter.model import OpenRouterModel, _resolve_model_settings

_PINNED_PROVIDER = "DeepSeek"
_PIN_MODEL_PREFIX = "deepseek/"


class OpenRouterDeepseekModel(OpenRouterModel):
    """OpenRouter model pinned to DeepSeek, with a per-tier reasoning policy.

    The provider stamps ``reasoning_setting`` per tier after construction (e.g.
    ``{"effort": "xhigh"}`` for the capable tier or ``{"enabled": False}`` for
    the cheap tier); a sensible default (reasoning on, xhigh) applies if unset.
    """

    reasoning_setting: dict = {"effort": "xhigh"}

    def _inject_pin(self, args: tuple, kwargs: dict) -> None:
        model_name = str(getattr(self, "model_name", "") or "")
        if not model_name.startswith(_PIN_MODEL_PREFIX):
            return
        settings = _resolve_model_settings(args, kwargs)
        if settings is None:
            return
        extra_body = dict(settings.get("extra_body") or {})
        if "provider" in extra_body:
            return  # caller set explicit routing — respect it
        extra_body["provider"] = {
            "only": [_PINNED_PROVIDER],
            "allow_fallbacks": False,
        }
        if "reasoning" not in extra_body:
            extra_body["reasoning"] = dict(self.reasoning_setting)
        settings["extra_body"] = extra_body

    async def _completions_create(self, *args: Any, **kwargs: Any) -> Any:
        self._inject_pin(args, kwargs)
        # OpenRouterModel adds usage.include + records cost.
        return await super()._completions_create(*args, **kwargs)
