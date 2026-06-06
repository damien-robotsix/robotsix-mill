"""OpenRouter transport layer (model-family agnostic).

The model/provider (which import pydantic-ai and thus opentelemetry) are loaded
lazily via PEP 562 ``__getattr__`` so importing the lightweight ``transient``
helpers does not drag in pydantic-ai/OTel at module load.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .transient import is_openrouter_transient, is_openrouter_upstream_error

if TYPE_CHECKING:  # static-only: real module-scope names for type checkers / CodeQL
    from .model import OpenRouterModel, record_openrouter_cost
    from .provider import OpenRouterProvider
    from .provider_cost import (
        KeyUsage,
        OpenRouterKeyCostSource,
        OpenRouterProviderCostSource,
    )

__all__ = [
    "KeyUsage",
    "OpenRouterKeyCostSource",
    "OpenRouterModel",
    "OpenRouterProvider",
    "OpenRouterProviderCostSource",
    "is_openrouter_transient",
    "is_openrouter_upstream_error",
    "record_openrouter_cost",
]


def __getattr__(name: str) -> Any:  # PEP 562 — lazy heavy imports
    if name == "OpenRouterModel":
        from .model import OpenRouterModel

        return OpenRouterModel
    if name == "record_openrouter_cost":
        from .model import record_openrouter_cost

        return record_openrouter_cost
    if name == "OpenRouterProvider":
        from .provider import OpenRouterProvider

        return OpenRouterProvider
    if name == "OpenRouterProviderCostSource":
        from .provider_cost import OpenRouterProviderCostSource

        return OpenRouterProviderCostSource
    if name == "OpenRouterKeyCostSource":
        from .provider_cost import OpenRouterKeyCostSource

        return OpenRouterKeyCostSource
    if name == "KeyUsage":
        from .provider_cost import KeyUsage

        return KeyUsage
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
