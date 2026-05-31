"""Derived DeepSeek-on-OpenRouter layer.

Requires the ``openrouter_deepseek`` extra (which pulls the OpenRouter
transport deps). The model/provider are loaded lazily via PEP 562
``__getattr__`` so importing the lightweight ``transient`` helpers stays free of
pydantic-ai / opentelemetry; a missing extra surfaces a clear install hint when
the model/provider is actually used.
"""

from __future__ import annotations

from typing import Any

from .transient import (
    is_deepseek_reasoning_roundtrip_error,
    is_deepseek_transient,
)

__all__ = [
    "OpenRouterDeepseekProvider",
    "OpenRouterDeepseekModel",
    "is_deepseek_transient",
    "is_deepseek_reasoning_roundtrip_error",
]


def __getattr__(name: str) -> Any:  # PEP 562 — lazy heavy imports
    if name in ("OpenRouterDeepseekProvider", "OpenRouterDeepseekModel"):
        try:
            if name == "OpenRouterDeepseekProvider":
                from .provider import OpenRouterDeepseekProvider

                return OpenRouterDeepseekProvider
            from .model import OpenRouterDeepseekModel

            return OpenRouterDeepseekModel
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "robotsix_llmio.openrouter_deepseek requires the "
                "'openrouter_deepseek' extra. Install with: "
                "pip install 'robotsix-llmio[openrouter_deepseek]'"
            ) from exc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
