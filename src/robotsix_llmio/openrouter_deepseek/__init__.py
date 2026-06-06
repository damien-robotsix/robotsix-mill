"""Derived DeepSeek-on-OpenRouter layer.

Requires the ``openrouter_deepseek`` extra (which pulls the OpenRouter
transport deps). The model/provider are loaded lazily via PEP 562
``__getattr__`` so a missing extra surfaces a clear install hint only when the
model/provider is actually used. Transient retry is inherited from the
OpenRouter layer (this layer adds no DeepSeek-specific transient signature).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # static-only: real module-scope names for type checkers / CodeQL
    from .model import OpenRouterDeepseekModel
    from .provider import OpenRouterDeepseekProvider

__all__ = [
    "OpenRouterDeepseekModel",
    "OpenRouterDeepseekProvider",
]

_DEEPSEEK_INSTALL_HINT = (
    "robotsix_llmio.openrouter_deepseek requires the "
    "'openrouter_deepseek' extra. Install with: "
    "pip install 'robotsix-llmio[openrouter_deepseek]'"
)


def __getattr__(name: str) -> Any:  # PEP 562 — lazy heavy imports
    # One explicit top-level guard per export so CodeQL's static export
    # analysis (py/undefined-export) can resolve each name to a real import.
    if name == "OpenRouterDeepseekModel":
        try:
            from .model import OpenRouterDeepseekModel
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_DEEPSEEK_INSTALL_HINT) from exc
        return OpenRouterDeepseekModel
    if name == "OpenRouterDeepseekProvider":
        try:
            from .provider import OpenRouterDeepseekProvider
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_DEEPSEEK_INSTALL_HINT) from exc
        return OpenRouterDeepseekProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
