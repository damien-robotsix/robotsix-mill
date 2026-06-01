"""DeepSeek-on-OpenRouter model — provider pin + per-tier reasoning policy +
reasoning round-trip.

Extends the OpenRouter transport model with DeepSeek's thinking-mode quirks:
- pin the upstream provider to DeepSeek (warms the per-provider prompt cache and
  keeps routing deterministic);
- inject a per-tier reasoning policy into the request (set by the provider:
  ``{"effort": "xhigh"}`` for the capable tier, ``{"enabled": False}`` for the
  cheap tier);
- carry ``reasoning_content`` on every assistant tool-call turn so a thinking-
  mode request is accepted.

Why the round-trip is needed: DeepSeek's capable tier runs in thinking mode and
raises HTTP 400 ("The `reasoning_content` in the thinking mode must be passed
back to the API.") whenever the request carries an assistant ``tool_calls`` turn
with no ``reasoning_content``. pydantic-ai's native
``openai_chat_send_back_thinking_parts`` does NOT cover the case mill hits: a
history that ENDS at a pending tool-result and is continued
(``run_sync(None, message_history=…)``) — a pre-seeded ``read_file`` batch
(``build_preseed_history``), a replayed ``conversation_state``, or a pause/resume
mid tool-loop. Those assistant tool-call turns are synthetic or reconstructed
and carry no reasoning, so they 400. Reproduced live in
``tests/openrouter_deepseek/test_openrouter_deepseek_live.py``
(``test_pro_resume_from_pending_tool_return_does_not_400``).

The fix: on the reasoning tier, every assistant tool-call turn carries a
``reasoning_content`` STRING — the turn's real reasoning (its ``ThinkingPart``s)
when present, else an empty string. DeepSeek requires the field to be a string;
an empty/placeholder string is accepted, a ``reasoning_details`` array is NOT
(both verified live). On the disabled (cheap) tier, all reasoning is stripped so
the sequence is consistently reasoning-free.
"""

from __future__ import annotations

from typing import Any

from ..openrouter.model import OpenRouterModel, _resolve_model_settings

_PINNED_PROVIDER = "DeepSeek"
_PIN_MODEL_PREFIX = "deepseek/"


def _reasoning_text(message: Any) -> str:
    """Concatenate the message's ``ThinkingPart`` contents into a string (the
    reasoning DeepSeek wants echoed back). Empty when the turn has no reasoning
    — e.g. a synthetic pre-seeded or reconstructed tool-call turn."""
    from pydantic_ai.messages import ThinkingPart

    parts = getattr(message, "parts", None) or []
    return "".join(
        p.content
        for p in parts
        if isinstance(p, ThinkingPart) and isinstance(getattr(p, "content", None), str)
    )


class OpenRouterDeepseekModel(OpenRouterModel):
    """OpenRouter model pinned to DeepSeek, with a per-tier reasoning policy and
    the thinking-mode ``reasoning_content`` round-trip.

    The provider stamps ``reasoning_setting`` per tier after construction (e.g.
    ``{"effort": "xhigh"}`` for the capable tier or ``{"enabled": False}`` for
    the cheap tier); a sensible default (reasoning on, xhigh) applies if unset.
    The round-trip is active on every tier except the disabled one, derived from
    ``reasoning_setting`` (no separate flag needed).
    """

    reasoning_setting: dict = {"effort": "xhigh"}

    @property
    def _echo_reasoning(self) -> bool:
        """Carry reasoning_content on tool-call turns iff reasoning is enabled —
        i.e. every tier except the cheap one's ``{"enabled": False}``."""
        return self.reasoning_setting.get("enabled", True) is not False

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

    def _map_model_response(self, message: Any) -> Any:
        """Map a ModelResponse to an OpenAI assistant message, enforcing
        DeepSeek's thinking-mode reasoning rule (see module docstring).

        Reasoning tier: assistant tool-call turns carry ``reasoning_content`` (a
        string — the turn's real reasoning, else empty); non-tool-call turns and
        the disabled tier carry no reasoning at all. The ``reasoning`` /
        ``reasoning_details`` variants are always dropped (DeepSeek rejects an
        array; only the string ``reasoning_content`` is accepted)."""
        param = super()._map_model_response(message)
        if not (isinstance(param, dict) and param.get("role") == "assistant"):
            return param

        # Always clear the array/alias forms — DeepSeek only accepts the string.
        param.pop("reasoning", None)  # type: ignore[typeddict-item]  # DeepSeek-specific field not in OpenAI stubs
        param.pop("reasoning_details", None)  # type: ignore[typeddict-item,misc]  # DeepSeek-specific field not in OpenAI stubs

        if self._echo_reasoning and param.get("tool_calls"):
            # Present-but-possibly-empty string keeps the tool-call turn valid in
            # thinking mode even when the turn is synthetic/reconstructed.
            param["reasoning_content"] = _reasoning_text(message)  # type: ignore[typeddict-unknown-key]  # DeepSeek-specific field not in OpenAI stubs
        else:
            param.pop("reasoning_content", None)  # type: ignore[typeddict-item]  # DeepSeek-specific field not in OpenAI stubs
        return param
