"""Claude Agent SDK transport layer (subscription / ``claude login`` auth).

Requires the ``claude_sdk`` extra plus a logged-in ``claude`` CLI and Node.js at
runtime. The model/provider are loaded lazily via PEP 562 ``__getattr__`` so
importing the lightweight ``transient`` helpers stays free of the SDK; a missing
extra surfaces a clear install hint when the model/provider is actually used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .transient import is_claude_sdk_transient, is_claude_sdk_turn_limit

if TYPE_CHECKING:  # static-only: real module-scope names for type checkers / CodeQL
    from .model import (
        ClaudeSDKModel,
        ClaudeSDKQueryTimeout,
        ClaudeSDKTurnLimitError,
    )
    from .provider import ClaudeSDKProvider

__all__ = [
    "ClaudeSDKModel",
    "ClaudeSDKProvider",
    "ClaudeSDKQueryTimeout",
    "ClaudeSDKTurnLimitError",
    "is_claude_sdk_transient",
    "is_claude_sdk_turn_limit",
]

_CLAUDE_SDK_INSTALL_HINT = (
    "robotsix_llmio.claude_sdk requires the 'claude_sdk' extra. "
    "Install with: pip install 'robotsix-llmio[claude_sdk]' "
    "(also needs Node.js and a logged-in `claude` CLI)."
)


def __getattr__(name: str) -> Any:  # PEP 562 — lazy heavy imports
    # One explicit top-level guard per export so CodeQL's static export
    # analysis (py/undefined-export) can resolve each name to a real import.
    if name == "ClaudeSDKModel":
        try:
            from .model import ClaudeSDKModel
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_CLAUDE_SDK_INSTALL_HINT) from exc
        return ClaudeSDKModel
    if name == "ClaudeSDKQueryTimeout":
        try:
            from .model import ClaudeSDKQueryTimeout
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_CLAUDE_SDK_INSTALL_HINT) from exc
        return ClaudeSDKQueryTimeout
    if name == "ClaudeSDKTurnLimitError":
        try:
            from .model import ClaudeSDKTurnLimitError
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_CLAUDE_SDK_INSTALL_HINT) from exc
        return ClaudeSDKTurnLimitError
    if name == "ClaudeSDKProvider":
        try:
            from .provider import ClaudeSDKProvider
        except ImportError as exc:  # pragma: no cover
            raise ImportError(_CLAUDE_SDK_INSTALL_HINT) from exc
        return ClaudeSDKProvider
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
