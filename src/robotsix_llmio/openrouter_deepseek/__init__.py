"""Derived DeepSeek-on-OpenRouter layer.

Requires the ``openrouter_deepseek`` extra (which pulls the OpenRouter
transport deps). Importing without it raises a clear install hint.
"""

from __future__ import annotations

try:
    from .model import OpenRouterDeepseekModel
    from .provider import OpenRouterDeepseekProvider
    from .transient import (
        is_deepseek_reasoning_roundtrip_error,
        is_deepseek_transient,
    )
except ImportError as exc:  # pragma: no cover — surfaced only on bad install
    raise ImportError(
        "robotsix_llmio.openrouter_deepseek requires the 'openrouter_deepseek' "
        "extra. Install with: pip install 'robotsix-llmio[openrouter_deepseek]'"
    ) from exc

__all__ = [
    "OpenRouterDeepseekProvider",
    "OpenRouterDeepseekModel",
    "is_deepseek_transient",
    "is_deepseek_reasoning_roundtrip_error",
]
