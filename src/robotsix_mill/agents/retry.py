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

import asyncio
import logging
import random
import time
from typing import Awaitable, Callable, TypeVar

from robotsix_llmio.core import call_with_retry as _lib_call_with_retry
from robotsix_llmio.core import constants as _constants
from robotsix_llmio.core import is_rate_limited
from robotsix_llmio.core.retry import _status
from robotsix_llmio.openrouter.transient import (
    is_openrouter_transient as is_transient,
)
from robotsix_llmio.openrouter.transient import (
    is_openrouter_upstream_error as _is_openrouter_upstream_error,
)

# NOTE: is_deepseek_reasoning_roundtrip_error was removed from robotsix-llmio
# (OpenRouter no longer raises the DeepSeek thinking-mode 400 when reasoning is
# stripped from a tool-call turn), so it is no longer imported or re-exported.

T = TypeVar("T")

log = logging.getLogger("robotsix_mill.agents.retry")

__all__ = [
    "call_with_retry",
    "acall_with_retry",
    "is_transient",
    "is_rate_limited",
    "_status",
    "_is_openrouter_upstream_error",
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


async def acall_with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    settings: object | None = None,
    what: str = "model call",
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    fallback_fn: Callable[[], Awaitable[T]] | None = None,
) -> T:
    """Async sibling of :func:`call_with_retry`.

    Mirrors the library's retry schedule (the baked ``TRANSIENT_*`` constants +
    the OpenRouter transient classifier and rate-limit fallback semantics), but
    ``await``s an async *fn* and uses ``asyncio.sleep`` for backoff. This lets a
    nested sub-agent tool retry ``await agent.run(...)`` on the coordinator's own
    running event loop, rather than calling ``asyncio.run`` (illegal inside the
    Claude SDK's already-running loop). *settings* is accepted for signature
    parity with :func:`call_with_retry` and is unused (constants are baked).
    """
    attempts = max(0, _constants.TRANSIENT_RETRIES)
    using_fallback = False
    for attempt in range(attempts + 1):
        try:
            if using_fallback:
                assert fallback_fn is not None  # type-narrowing
                return await fallback_fn()
            return await fn()
        except Exception as e:  # noqa: BLE001 — re-raised unless retryable
            if attempt >= attempts:
                raise
            if is_transient(e):
                delay = min(
                    _constants.TRANSIENT_BACKOFF_CAP,
                    _constants.TRANSIENT_BACKOFF_BASE * (2**attempt),
                )
                delay += random.uniform(0, delay / 2)  # jitter
                log.warning(
                    "%s: transient %s (attempt %d/%d) — retrying in %.1fs",
                    what,
                    type(e).__name__,
                    attempt + 1,
                    attempts,
                    delay,
                )
                await sleep(delay)
                continue
            if is_rate_limited(e):
                if not using_fallback and fallback_fn is not None:
                    using_fallback = True
                    log.warning(
                        "%s: rate-limit fallback activated on first UsageLimitExceeded",
                        what,
                    )
                    continue  # try fallback immediately, same attempt slot
                raise
            # non-retryable
            raise
    raise AssertionError("unreachable")  # pragma: no cover
