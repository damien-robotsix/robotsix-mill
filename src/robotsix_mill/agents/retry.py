"""Compatibility shim — retry/backoff now lives in the robotsix-llmio library.

``call_with_retry`` + the transient/rate-limit classifiers were extracted into
``robotsix-llmio`` (``core`` + the provider layers). This module preserves the
historical mill API: the retry/backoff constants are baked in the library
(which equal mill's former defaults), and the public classifier names are
re-exported.

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
from typing import Any, Awaitable, Callable, TypeVar

from robotsix_llmio.claude_sdk.transient import (
    is_claude_sdk_transient as _is_claude_sdk_transient,
)
from robotsix_llmio.core import call_with_retry as _lib_call_with_retry
from robotsix_llmio.core import constants as _constants
from robotsix_llmio.core import is_rate_limited
from robotsix_llmio.core.retry import _status
from robotsix_llmio.openrouter.transient import (
    is_openrouter_transient as _is_openrouter_transient,
)
from robotsix_llmio.openrouter.transient import (
    is_openrouter_upstream_error as _is_openrouter_upstream_error,
)


def _is_claude_sdk_degenerate_result(exc: BaseException) -> bool:
    """Recognise the degenerate ``is_error=True`` + ``subtype="success"`` result.

    When the ``claude`` CLI emits a ``result`` frame that is self-contradictory
    (``is_error=True`` but an empty ``errors`` list and ``subtype="success"``),
    the claude_agent_sdk computes its message as ``"; ".join(errors) or
    str(subtype)`` → ``"success"`` and **replaces** the underlying ``ProcessError``
    with a bare ``Exception("Claude Code returned an error result: success")``.
    That erases the ``ProcessError`` type, so ``_is_claude_sdk_transient`` (which
    matches by exception TYPE NAME) cannot see it. A string match on the message
    is the only mechanism left — mirroring the library's string-based
    ``is_claude_sdk_turn_limit`` approach. We walk the cause/context chain
    (bounded) and match narrowly on the ``...: success`` contradiction only, so a
    genuine ``error_during_execution`` / ``error_max_turns`` result still surfaces
    as non-transient."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    for _ in range(10):
        if cur is None or id(cur) in seen:
            break
        seen.add(id(cur))
        if "returned an error result: success" in str(cur).lower():
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def is_transient(exc: BaseException) -> bool:
    """Transient if EITHER backend's classifier says so.

    mill runs both the OpenRouter/DeepSeek and the Claude SDK transports, so a
    single retry predicate must recognise both families: OpenRouter
    429/5xx/upstream on the DeepSeek path, and Claude SDK subprocess/connection/
    query-timeout failures on the Claude path. The two sets don't overlap in
    practice, so OR-ing them keeps local retries correct for whichever backend
    actually ran — previously only OpenRouter errors were retried, so a Claude
    CLI hiccup or query timeout skipped local retry entirely.

    The degenerate Claude SDK ``success`` result (``is_error=True`` with
    ``subtype='success'``) was historically treated as a structural
    misconfiguration and fast-failed, but observed behaviour shows it is
    transient — a fresh run on the same input succeeds normally at similar
    cost, so retrying it is cheaper than blocking and re-running the whole
    stage."""
    if _is_claude_sdk_degenerate_result(exc):
        log.warning(
            "Claude SDK degenerate 'success' result detected — "
            "retrying (transient in practice)"
        )
        return True
    return _is_openrouter_transient(exc) or _is_claude_sdk_transient(exc)


# NOTE: is_deepseek_reasoning_roundtrip_error was removed from robotsix-llmio
# (OpenRouter no longer raises the DeepSeek thinking-mode 400 when reasoning is
# stripped from a tool-call turn), so it is no longer imported or re-exported.

T = TypeVar("T")

log = logging.getLogger("robotsix_mill.agents.retry")

__all__ = [
    "call_with_retry",
    "acall_with_retry",
    "run_agent",
    "is_transient",
    "is_rate_limited",
    "_status",
    "_is_openrouter_upstream_error",
]


def call_with_retry(
    fn: Callable[[], T],
    *,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
    fallback_fn: Callable[[], T] | None = None,
) -> T:
    """Run ``fn`` with bounded transient/rate-limit retry."""
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
    Claude SDK's already-running loop).
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


def run_agent(
    agent: Any,
    make_run: Callable[[Any], T],
    *,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run *agent* with bounded local retry.

    *make_run* takes a handle and performs the actual run, e.g.
    ``lambda h: h.run_sync(prompt, message_history=hist, usage_limits=limits)``.
    The transport is fixed by the agent's level (no cross-backend fallback);
    transient errors retry on the same handle."""
    return _lib_call_with_retry(
        lambda: make_run(agent),
        what=what,
        sleep=sleep,
        is_transient_fn=is_transient,
    )
