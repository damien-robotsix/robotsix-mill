"""DeepSeek-on-OpenRouter model — provider pin + per-tier reasoning policy.

Extends the OpenRouter transport model with DeepSeek's thinking-mode quirks:
- pin the upstream provider to DeepSeek (warms the per-provider prompt cache);
- apply a per-instance reasoning policy (set by the provider per tier) instead
  of guessing from the model name;
- round-trip ``reasoning_details`` so thinking mode accepts follow-up/tool-call
  turns when reasoning is enabled, and strip all reasoning when it's disabled —
  keeping the echoed sequence consistent so the thinking-mode 400 can't fire.

See robotsix-mill PRs #523/#529/#531 for the behavior this preserves.
"""

from __future__ import annotations

from typing import Any

from ..openrouter.model import OpenRouterModel, _resolve_model_settings

_PINNED_PROVIDER = "DeepSeek"
_PIN_MODEL_PREFIX = "deepseek/"
_REASONING_DETAILS_KEY = "reasoning_details"
# Field-present-but-empty entry: keeps every tool-call turn carrying a
# reasoning_details field (consistent "all-present" sequence) when the model
# emitted none on a turn. Verified accepted by DeepSeek flash/pro.
_EMPTY_REASONING = [{"type": "reasoning.text", "text": "", "format": "unknown"}]


def _extract_reasoning_details(response: Any) -> Any:
    """Pull the raw ``reasoning_details`` array off an OpenRouter chat
    completion message, or ``None``. Handles the str-response branch and the
    OpenAI SDK ``model_extra`` stash."""
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


class OpenRouterDeepseekModel(OpenRouterModel):
    """OpenRouter model with the DeepSeek pin + reasoning round-trip.

    The provider stamps two attributes per tier after construction:
    - ``reasoning_setting``: e.g. ``{"effort": "xhigh"}`` or ``{"enabled": False}``
    - ``echo_reasoning``: whether to round-trip reasoning_details (True for the
      reasoning tier, False for the disabled tier).
    Sensible defaults (reasoning on, xhigh) apply if unset.
    """

    reasoning_setting: dict = {"effort": "xhigh"}
    echo_reasoning: bool = True

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

        # Reasoning-disabled tier: strip ALL reasoning from every turn so the
        # request is consistently reasoning-free (no thinking-mode mix-400).
        if not self.echo_reasoning:
            param.pop("reasoning", None)
            param.pop("reasoning_content", None)
            param.pop(_REASONING_DETAILS_KEY, None)
            return param

        # Reasoning tier: echo reasoning_details ONLY on tool-call turns; omit
        # on non-tool-call turns (DeepSeek thinking-mode rule).
        if param.get("tool_calls"):
            rd = (message.provider_details or {}).get(_REASONING_DETAILS_KEY)
            param.pop("reasoning", None)
            param.pop("reasoning_content", None)
            # Present-but-empty when the model emitted no reasoning this turn,
            # so the sequence stays "all-present" rather than a failing mix.
            param[_REASONING_DETAILS_KEY] = rd if rd else list(_EMPTY_REASONING)
        else:
            param.pop("reasoning", None)
            param.pop("reasoning_content", None)
            param.pop(_REASONING_DETAILS_KEY, None)
        return param
