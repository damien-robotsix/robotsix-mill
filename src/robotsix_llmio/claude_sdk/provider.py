"""Claude Agent SDK provider — subscription-auth transport, one model per tier.

Sibling of the OpenRouter layer (both derive from :class:`core.LLMProvider`),
but it speaks to no HTTP endpoint: it drives the local ``claude`` CLI via the
Claude Agent SDK, so it needs **no API key** — only a logged-in ``claude``
(``claude login``) and Node.js on PATH.

The only consumer knob is the :class:`~robotsix_llmio.core.Tier`; the tier→model
map is baked (overridable at construction for experimentation).
"""

from __future__ import annotations

from typing import Any

from ..core.provider import LLMProvider, Tier
from .transient import is_claude_sdk_transient

# Baked tier→model map. Values are Claude Code model aliases passed straight to
# the SDK's ``model`` option (it resolves them to the latest concrete model).
_DEFAULT_MODEL = "opus"
_CHEAP_MODEL = "haiku"


class ClaudeSDKProvider(LLMProvider):
    """Builds :class:`~robotsix_llmio.claude_sdk.model.ClaudeSDKModel` instances,
    one per tier, authenticated by your ``claude login`` subscription."""

    def __init__(
        self,
        *,
        default_model: str = _DEFAULT_MODEL,
        cheap_model: str = _CHEAP_MODEL,
    ) -> None:
        self._models = {Tier.DEFAULT: default_model, Tier.CHEAP: cheap_model}

    def new_model(self, tier: Tier = Tier.DEFAULT) -> tuple[Any, Any]:
        from .model import ClaudeSDKModel

        # No http_client to manage — the CLI subprocess is the transport, and
        # the SDK tears it down per call. AgentHandle.close() tolerates None.
        return ClaudeSDKModel(self._models[tier]), None

    def _is_transient(self, exc: BaseException) -> bool:
        return is_claude_sdk_transient(exc)
