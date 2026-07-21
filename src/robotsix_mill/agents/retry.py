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
import concurrent.futures
import logging
import random
import time
from typing import Any, Awaitable, Callable, TypeVar

from robotsix_llmio.claude_sdk.transient import (
    is_claude_sdk_transient as _is_claude_sdk_transient,
)
from robotsix_llmio.core import (
    acall_with_retry_and_fallback,
    call_with_retry as _lib_call_with_retry,
    call_with_retry_and_fallback,
)
from robotsix_llmio.core import constants as _constants
from robotsix_llmio.core import is_rate_limited
from robotsix_llmio.openrouter.transient import (
    is_openrouter_transient as _is_openrouter_transient,
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
    ``is_claude_sdk_turn_limit`` approach.

    This detector is NOT used for retry/transient classification — observed
    behaviour shows the degenerate result is deterministic for a given input (a
    fresh run on the same input produces the same result).  Instead, the refine
    runner catches it at the agent-output level and treats it as a successful
    empty result, since ``subtype="success"`` and an empty errors list indicate
    the CLI completed normally and the error envelope is a false positive.
    """
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
    ``subtype='success'``) is explicitly excluded — it is deterministic for a
    given input. The refine runner catches it at the agent-output level and
    treats it as a successful empty result.
    """
    if _is_claude_sdk_degenerate_result(exc):
        return False
    return _is_openrouter_transient(exc) or _is_claude_sdk_transient(exc)


# NOTE: is_deepseek_reasoning_roundtrip_error was removed from robotsix-llmio
# (OpenRouter no longer raises the DeepSeek thinking-mode 400 when reasoning is
# stripped from a tool-call turn), so it is no longer imported or re-exported.

T = TypeVar("T")

log = logging.getLogger("robotsix_mill.agents.retry")


def _try_record_step_usage(
    result: Any,
    retry_count: int = 0,
    retry_reason: str = "",
) -> None:
    """Extract per-step usage from a pydantic-ai result and record it as a
    span attribute.  Best-effort: silently returns on any failure so a
    non-pydantic-ai result or a missing OTel span never blocks the caller.
    """
    try:
        usage = result.usage()
        model_name: str = getattr(result.response, "model_name", "") or ""
        request_count: int = usage.requests
        input_tokens: int = usage.input_tokens
        output_tokens: int = usage.output_tokens
        cache_read_tokens: int = getattr(usage, "cache_read_tokens", 0) or 0
        cache_write_tokens: int = getattr(usage, "cache_write_tokens", 0) or 0

        tool_calls: list[dict[str, Any]] = []
        try:
            for msg in result.all_messages():
                for part in msg.parts:
                    tool_name = getattr(part, "tool_name", None)
                    if tool_name:
                        args_raw = getattr(part, "args", None)
                        args_str = str(args_raw)[:200] if args_raw else ""
                        tool_calls.append({"name": str(tool_name), "args": args_str})
        except Exception:
            log.debug(
                "_try_record_step_usage: tool-call extraction failed", exc_info=True
            )

        # Detect billing backend from model name so the cost-analyst can
        # distinguish subscription (Claude SDK, flat cost) from pay-per-token
        # (OpenRouter, real marginal cost).  When model_name is empty the
        # result came from a Claude SDK tool agent (which has no .response).
        backend = ""
        if not model_name:
            backend = "claude_sdk"
        elif "openrouter" in model_name.lower():
            backend = "openrouter"
        elif model_name.lower().startswith("claude"):
            backend = "claude_sdk"

        from ..runtime.tracing import record_step_usage as _record

        _record(
            request_count=request_count,
            model_name=model_name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls if tool_calls else None,
            retry_count=retry_count,
            retry_reason=retry_reason,
            cache_read_input_tokens=cache_read_tokens,
            cache_creation_input_tokens=cache_write_tokens,
            backend=backend,
        )
    except Exception:
        log.debug("_try_record_step_usage: failed to record step usage", exc_info=True)


__all__ = [
    "call_with_retry",
    "acall_with_retry",
    "run_agent",
    "is_transient",
    "is_rate_limited",
]


def call_with_retry(
    fn: Callable[[], T],
    *,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
    fallback_fn: Callable[[], T] | None = None,
) -> T:
    """Run ``fn`` with bounded transient/rate-limit retry.

    When called from within a running event loop (e.g. worker processing on
    Python >=3.14), the library's ``call_with_retry`` cannot use
    ``asyncio.run()`` (RuntimeError).  In that case the call is delegated to a
    thread so the library can create its own event loop safely.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to use asyncio.run() directly.
        return _lib_call_with_retry(
            fn,
            what=what,
            sleep=sleep,
            fallback_fn=fallback_fn,
            is_transient_fn=is_transient,
        )

    # Running loop detected — delegate to a thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(
            _lib_call_with_retry,
            fn,
            what=what,
            sleep=sleep,
            fallback_fn=fallback_fn,
            is_transient_fn=is_transient,
        )
        return fut.result()


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

    When *fallback_fn* is provided, the primary is retried locally first.  Only
    when local retries are exhausted does the fallback run, itself through a
    fresh retry session.  This guards against persistent provider-side outages
    (e.g. DeepSeek 503 on OpenRouter) by falling back to a different model.

    After a successful run, per-step usage data is recorded on the current OTel
    span (same contract as :func:`run_agent`).
    """
    if fallback_fn is not None:
        result = await acall_with_retry_and_fallback(
            fn,
            fallback_fn,
            what=what,
            sleep=sleep,
            is_transient_primary=is_transient,
            is_transient_fallback=is_transient,
        )
        _try_record_step_usage(result)
        return result

    attempts = max(0, _constants.TRANSIENT_RETRIES)
    using_fallback = False
    retry_count = 0
    last_reason = ""
    for attempt in range(attempts + 1):
        try:
            if using_fallback:
                assert fallback_fn is not None  # type-narrowing
                result = await fallback_fn()
            else:
                result = await fn()
            # Record per-step usage on success.
            _try_record_step_usage(result, retry_count, last_reason)
            return result
        except Exception as e:  # noqa: BLE001 — re-raised unless retryable
            if attempt >= attempts:
                raise
            if is_transient(e):
                retry_count += 1
                last_reason = f"{type(e).__name__}: {e!s}"[:200]
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
    fallback_fn: Callable[[], T] | None = None,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run *agent* with bounded local retry.

    *make_run* takes a handle and performs the actual run, e.g.
    ``lambda h: h.run_sync(prompt, message_history=hist, usage_limits=limits)``.
    The transport is fixed by the agent's level; transient errors retry on the
    same handle.

    When *fallback_fn* is provided, the primary agent is retried locally
    first (same transient/backoff schedule).  Only when local retries are
    exhausted does the fallback run, itself through a fresh retry session.
    This guards against persistent provider-side outages (e.g. DeepSeek 503
    on OpenRouter) by falling back to a different model.

    After a successful run, per-step usage data (token counts, model name,
    request count, tool calls, and retry info) is recorded as a span
    attribute on the current OTel span so the trace inspector and
    cost-analyst can attribute spend without fetching every Langfuse
    observation.

    When called from within a running event loop (e.g. worker processing on
    Python >=3.14, or a tool on the Claude SDK's loop), *make_run* typically
    ends in ``run_sync`` → ``asyncio.run()``, which raises RuntimeError.  As in
    :func:`call_with_retry`, the whole retry session is then delegated to a
    thread so a fresh event loop can be created safely.
    """
    retry_count = 0
    last_reason = ""

    def _primary() -> T:
        nonlocal retry_count, last_reason
        try:
            return make_run(agent)
        except Exception as e:
            if is_transient(e):
                retry_count += 1
                last_reason = f"{type(e).__name__}: {e!s}"[:200]
            raise

    if fallback_fn is not None:

        def _call() -> T:
            return call_with_retry_and_fallback(
                _primary,
                fallback_fn,
                what=what,
                sleep=sleep,
                is_transient_primary=is_transient,
                is_transient_fallback=is_transient,
            )

    else:

        def _call() -> T:
            return _lib_call_with_retry(
                _primary,
                what=what,
                sleep=sleep,
                is_transient_fn=is_transient,
            )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — safe to run on this thread.
        result = _call()
    else:
        # Running loop detected — delegate to a thread.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            result = ex.submit(_call).result()

    # Record per-step usage as a span attribute when the result is a
    # pydantic-ai AgentRunResult (has .usage() and .all_messages()).
    _try_record_step_usage(result, retry_count, last_reason)
    return result
