"""DeepSeek-specific transient signatures, layered on the OpenRouter set."""

from __future__ import annotations

from ..core.retry import _status, _walk_cause_chain
from ..openrouter.transient import is_openrouter_transient


def is_deepseek_reasoning_roundtrip_error(exc: BaseException) -> bool:
    """Detect the DeepSeek thinking-mode reasoning round-trip 400.

    When pinned to DeepSeek's first-party provider (to warm the prompt cache),
    the model can intermittently emit an inconsistent reasoning sequence and
    the API responds with HTTP 400 about ``reasoning_content`` needing to be
    passed back. A fresh re-run usually yields a clean sequence, so treat it
    as transient. Narrowly matched on the distinctive marker so other genuine
    400s stay non-transient.
    """
    if _status(exc) != 400:
        return False
    for cur in _walk_cause_chain(exc):
        msg = str(cur)
        if "reasoning_content" in msg and "passed back" in msg:
            return True
    return False


def is_deepseek_transient(exc: BaseException) -> bool:
    """OpenRouter transient set OR the DeepSeek reasoning round-trip 400."""
    return is_openrouter_transient(exc) or is_deepseek_reasoning_roundtrip_error(exc)
