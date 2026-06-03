"""OpenRouter transport layer (model-family agnostic).

The model/provider (which import pydantic-ai and thus opentelemetry) are loaded
lazily via PEP 562 ``__getattr__`` so importing the lightweight ``transient``
helpers does not drag in pydantic-ai/OTel at module load.
"""

from __future__ import annotations

from typing import Any

from .transient import is_openrouter_transient, is_openrouter_upstream_error

__all__ = [
    "OpenRouterModel",
    "OpenRouterProvider",
    "OpenRouterProviderCostSource",
    "record_openrouter_cost",
    "is_openrouter_transient",
    "is_openrouter_upstream_error",
]


def __getattr__(name: str) -> Any:  # PEP 562 — lazy heavy imports
    if name in ("OpenRouterModel", "record_openrouter_cost"):
        from . import model

        return getattr(model, name)
    if name == "OpenRouterProvider":
        from .provider import OpenRouterProvider

        return OpenRouterProvider
    if name == "OpenRouterProviderCostSource":
        from .provider_cost import OpenRouterProviderCostSource

        return OpenRouterProviderCostSource
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
