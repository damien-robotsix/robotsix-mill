"""Compatibility shim — retry/backoff now lives in the robotsix-llmio library.

``call_with_retry`` + the transient/rate-limit classifiers were extracted into
``robotsix-llmio`` (``core`` + the provider layers). This module preserves the
historical mill API: the retry/backoff constants are baked in the library
(which equal mill's former defaults), and the public classifier names are
re-exported.

The call-level retry predicate is the OpenRouter transient set (429/5xx/timeout/
malformed-JSON/upstream-error) — deliberately NOT provider-specific reasoning-400,
which surfaces to the worker's stage-retry (a fresh re-run) rather than being
retried in the same conversation. ``classify_stage_error`` picks up the
reasoning-400 via the re-exported detector.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

from robotsix_llmio.claude_sdk.transient import (
    is_claude_sdk_permanent_api_error as _is_permanent_api_error,
)
from robotsix_llmio.claude_sdk.transient import (
    is_claude_sdk_transient as _is_claude_sdk_transient,
)
from robotsix_llmio.core import (
    acall_with_retry_and_fallback,
    call_with_retry_and_fallback,
    is_rate_limited,
)
from robotsix_llmio.core import (
    call_with_retry as _lib_call_with_retry,
)
from robotsix_llmio.core import constants as _constants
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

    That "false positive" reading holds only when the frame carries no real
    error. A **permanent API error takes precedence**: when the CLI reports an
    API 400 as assistant text, the SDK collapses that frame into this very same
    degenerate message, so the signature matches even though the call genuinely
    failed and can never succeed. Treating it as an empty success made refine
    silently no-op on a config error (a `task_budget.total` below the API's
    20,000 floor) — worse than failing, because nothing alerted. The library
    owns that classification (``is_claude_sdk_permanent_api_error``); this only
    defers to it.
    """
    if _is_permanent_api_error(exc):
        return False
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

    mill runs both the OpenRouter and Claude SDK transports, so a
    single retry predicate must recognise both families: OpenRouter
    429/5xx/upstream on the OpenRouter path, and Claude SDK subprocess/connection/
    query-timeout failures on the Claude path. The two sets don't overlap in
    practice, so OR-ing them keeps local retries correct for whichever backend
    actually ran — previously only OpenRouter errors were retried, so a Claude
    CLI hiccup or query timeout skipped local retry entirely.

    An API ``400`` (request validation — every retry re-sends the identical
    rejected payload) needs no guard here: ``is_claude_sdk_transient`` already
    excludes it, ahead of the degenerate-``success`` signature the SDK collapses
    it into. That ordering lives in the library, so this function inherits it.

    The degenerate Claude SDK ``success`` result (``is_error=True`` with
    ``subtype='success'``) is excluded — it is deterministic for a given input.
    The refine runner catches it at the agent-output level and treats it as a
    successful empty result.

    Also recognises ``sqlite3.OperationalError`` containing "database or disk is
    full" — a host-level disk-full condition that cannot be resolved by retrying,
    so this classifier lets the worker's stage-level park mechanism kick in
    sooner instead of burning internal retries.
    """
    if _is_claude_sdk_degenerate_result(exc):
        return False
    from ..runtime.transient_errors import is_disk_full_error

    return (
        _is_openrouter_transient(exc)
        or _is_claude_sdk_transient(exc)
        or is_disk_full_error(exc)
    )


# NOTE: is_deepseek_reasoning_roundtrip_error was removed from robotsix-llmio
# (the upstream provider no longer raises the thinking-mode 400 when reasoning is
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
    "acall_with_retry",
    "call_with_retry",
    "closing_scratch_loop",
    "is_rate_limited",
    "is_transient",
    "run_agent",
]


@contextlib.contextmanager
def closing_scratch_loop() -> Iterator[None]:
    """Close the event loop ``pydantic_ai`` leaves installed on this thread.

    ``Agent.run_sync`` funnels through ``pydantic_ai._utils.get_event_loop``,
    which does ``asyncio.new_event_loop()`` + ``asyncio.set_event_loop()`` and
    never closes the loop.  ``BaseEventLoop.close()`` is what shuts a loop's
    default executor down, so an unclosed loop strands that executor's
    ``asyncio_N`` threads in ``futex_wait`` for the life of the process — each
    one costing an 8 MB stack reservation and its own glibc malloc arena.  On a
    long-lived worker thread (mill runs stages via ``asyncio.to_thread``) the
    loops accumulate until the container hits its memory cap and is OOM-killed,
    which kills in-flight sandboxes and silently burns implement spawn attempts.

    Only a loop this block is responsible for is closed: if a loop was already
    installed on the thread on entry it is left exactly as found, and a running
    loop is never touched.  Agents are built and closed per call (see
    ``agents.base``), so nothing loop-affine outlives the block.
    """
    before = _current_event_loop()
    try:
        yield
    finally:
        after = _current_event_loop()
        if after is not None and after is not before and not after.is_running():
            with contextlib.suppress(Exception):
                after.close()
            # Drop the thread-local reference so the next ``run_sync`` on this
            # thread installs a fresh loop rather than reusing a closed one.
            with contextlib.suppress(Exception):
                asyncio.set_event_loop(None)


def _current_event_loop() -> asyncio.AbstractEventLoop | None:
    """The event loop installed on this thread, or ``None`` if there is none.

    On Python >=3.14 ``asyncio.get_event_loop()`` raises rather than creating a
    loop as a side effect, so this is a pure read.
    """
    try:
        return asyncio.get_event_loop()
    except RuntimeError:
        return None


def call_with_retry[T](
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
        # No running loop — safe to use asyncio.run() directly.  This runs on
        # the caller's (often pooled, long-lived) thread, so any scratch loop
        # pydantic-ai installs here must be closed rather than stranded.
        with closing_scratch_loop():
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


async def acall_with_retry[T](
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
    (e.g. provider 503 on OpenRouter) by falling back to a different model.

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
        except Exception as e:
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


def run_agent[T](
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
    This guards against persistent provider-side outages (e.g. provider 503
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

    def _call_closing() -> T:
        with closing_scratch_loop():
            return _call()

    def _run_isolated(fn: Callable[[], T]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — safe to run on this thread.
            return fn()
        # Running loop detected — delegate to a thread.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(fn).result()

    try:
        result = _run_isolated(_call_closing)
    except Exception as exc:
        if not is_tier_unavailable(exc):
            raise
        result = _run_at_fallback_slot(
            agent, make_run, exc, what=what, sleep=sleep, run_isolated=_run_isolated
        )

    # Record per-step usage as a span attribute when the result is a
    # pydantic-ai AgentRunResult (has .usage() and .all_messages()).
    _try_record_step_usage(result, retry_count, last_reason)
    return result


def is_tier_unavailable(exc: BaseException) -> bool:
    """Whether *exc* means the agent's whole tier is out of service.

    Usage exhaustion (the subscription's session/weekly cap) and a dead OAuth
    credential are per-provider, not per-request: re-running at the same tier
    cannot help, and llmio deliberately never retries them (see
    ``ClaudeSDKUsageExhaustedError`` / ``ClaudeSDKAuthError``). They are the
    cases where the run should move to a *different* tier instead of failing.
    """
    from robotsix_llmio.claude_sdk._errors import (
        ClaudeSDKAuthError,
        ClaudeSDKUsageExhaustedError,
    )

    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ClaudeSDKUsageExhaustedError | ClaudeSDKAuthError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _run_at_fallback_slot[T](
    agent: Any,
    make_run: Callable[[Any], T],
    original: Exception,
    *,
    what: str,
    sleep: Callable[[float], None],
    run_isolated: Callable[[Callable[[], T]], T],
) -> T:
    """Rebuild *agent* on the fallback provider slot (same level) and run there.

    Only agents built by ``build_agent_from_definition`` under
    ``provider_failover_enabled`` carry the ``_failover_rebuild`` hook;
    anything else re-raises *original* unchanged. The fallback run gets its
    own bounded retry session.
    """
    from robotsix_llmio.core.failover import get_failover_tracker

    from .base import _safe_close

    rebuild = getattr(agent, "_failover_rebuild", None)
    if not callable(rebuild):
        raise original

    # Inform the process-wide failover tracker so subsequent builds resolve
    # the fallback slot directly (and the UI shows failover as active). Only
    # reached when ``provider_failover_enabled`` attached the rebuild hook.
    get_failover_tracker().record_failure("default", original)

    log.warning(
        "%s: default provider unavailable (%s: %s) — retrying on the "
        "fallback provider slot",
        what,
        type(original).__name__,
        str(original)[:160],
    )
    try:
        fallback_agent = rebuild()
    except Exception as build_exc:
        raise build_exc from original

    def _fallback_call(h: Any = fallback_agent) -> T:
        with closing_scratch_loop():
            return _lib_call_with_retry(
                lambda: make_run(h),
                what=f"{what} (provider-failover)",
                sleep=sleep,
                is_transient_fn=is_transient,
            )

    try:
        return run_isolated(_fallback_call)
    finally:
        _safe_close(fallback_agent)
