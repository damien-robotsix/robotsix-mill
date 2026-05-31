"""Compatibility shim — retry/backoff now lives in the robotsix-llmio library.

``call_with_retry`` + the transient/rate-limit classifiers were extracted into
``robotsix-llmio`` (``core`` + the provider layers). This module preserves the
historical mill API: the ``settings`` keyword is still accepted (the library
now bakes the retry/backoff constants, which equal mill's former defaults), and
the public classifier names are re-exported.

The call-level retry predicate is the OpenRouter transient set (429/5xx/timeout/
malformed-JSON/upstream-error) — deliberately NOT the DeepSeek reasoning-400,
which surfaces to the worker's stage-retry (a fresh re-run) rather than being
retried in the same conversation. ``classify_stage_error`` picks up the
reasoning-400 via the re-exported detector.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

from robotsix_llmio.core import call_with_retry as _lib_call_with_retry
from robotsix_llmio.core import is_rate_limited
from robotsix_llmio.core.retry import _status
from robotsix_llmio.openrouter.transient import (
    is_openrouter_transient as is_transient,
)
from robotsix_llmio.openrouter.transient import (
    is_openrouter_upstream_error as _is_openrouter_upstream_error,
)
# Import from the .transient submodule (NOT the package __init__) so this shim
# stays free of pydantic_ai/opentelemetry at module load — runtime.tracing's
# import chain reaches here and must not eagerly import OTel.
from robotsix_llmio.openrouter_deepseek.transient import (
    is_deepseek_reasoning_roundtrip_error as _is_deepseek_reasoning_roundtrip_error,
)

T = TypeVar("T")

__all__ = [
    "call_with_retry",
    "is_transient",
    "is_rate_limited",
    "_status",
    "_is_openrouter_upstream_error",
    "_is_deepseek_reasoning_roundtrip_error",
]


def call_with_retry(
    fn: Callable[[], T],
    *,
    settings: object | None = None,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
    fallback_fn: Callable[[], T] | None = None,
) -> T:
    """Run ``fn`` with bounded transient/rate-limit retry.

    *settings* is accepted for signature compatibility but no longer drives the
    schedule — the library bakes the (formerly mill-default) constants.
    """
    return _lib_call_with_retry(
        fn,
        what=what,
        sleep=sleep,
        fallback_fn=fallback_fn,
        is_transient_fn=is_transient,
    )
