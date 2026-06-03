"""Bounded retry + backoff for transient model/network failures and
rate-limit (``UsageLimitExceeded``) errors.

Transient = a ``ModelHTTPError`` with status 429 or 5xx, an httpx
timeout/transport error, an httpx 429/5xx response, a malformed-JSON decode,
or a provider-supplied extra signature (see ``is_transient_fn``). These ride
out with a short exponential backoff.

``UsageLimitExceeded`` is NOT transient (re-running immediately just re-hits
the cap) but IS rate-limited: handled with a longer schedule and an optional
one-shot fallback callable.

Everything else surfaces immediately, unchanged.

Parameters (retry counts, backoff) come from :mod:`.constants` — they are not
tunable per call.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, Iterator, TypeVar

from . import constants
from ._otel import get_recording_span
from .cost import flush_current_provider

log = logging.getLogger("robotsix_llmio.retry")

T = TypeVar("T")


def _walk_cause_chain(
    exc: BaseException, max_depth: int = 10
) -> Iterator[BaseException]:
    """Yield each exception in the cause/context chain, bounded to *max_depth*."""
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < max_depth:
        yield cur
        cur = cur.__cause__ or cur.__context__
        seen += 1


def _status(exc: BaseException) -> int | None:
    # pydantic-ai ModelHTTPError(status_code, ...) and httpx
    # HTTPStatusError(response.status_code) both expose a status.
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    rc = getattr(resp, "status_code", None)
    return rc if isinstance(rc, int) else None


# JSONDecodeError: the model occasionally emits malformed JSON for a tool call
# / structured output; a re-run almost always yields valid JSON, so treat it
# as transient instead of hard-failing.
_TRANSIENT_NAMES = {
    "APITimeoutError",
    "APIConnectionError",
    "JSONDecodeError",
}


def is_transient(exc: BaseException) -> bool:
    """True only for *generic* retryable infrastructure failures. Walks the
    cause/context chain so a timeout wrapped by openai/pydantic-ai
    (e.g. ModelHTTPError <- APITimeoutError <- httpx.ReadTimeout) is still
    recognised. Provider layers extend this with their own signatures."""
    import httpx

    for cur in _walk_cause_chain(exc):
        name = type(cur).__name__
        if name == "UsageLimitExceeded":
            return False  # budget cap — never transient
        if isinstance(cur, (httpx.TimeoutException, httpx.TransportError)):
            return True
        if name in _TRANSIENT_NAMES:
            return True
        code = _status(cur)
        if code is not None and (code == 429 or 500 <= code < 600):
            return True
    return False


def is_rate_limited(exc: BaseException) -> bool:
    """True only for ``UsageLimitExceeded`` (the pydantic-ai budget-cap
    exception). Walks the cause/context chain."""
    for cur in _walk_cause_chain(exc):
        if type(cur).__name__ == "UsageLimitExceeded":
            return True
    return False


def _record_rate_limit_span(
    count: int,
    cumulative_backoff: float,
    fallback_activated: bool,
) -> None:
    """Record rate-limit metrics on the current OTel span; no-op without OTel."""
    span = get_recording_span()
    if span is None:
        return
    span.set_attribute("llmio.rate_limit.count", count)
    span.set_attribute("llmio.rate_limit.backoff_seconds", cumulative_backoff)
    if fallback_activated:
        span.set_attribute("llmio.rate_limit.fallback_activated", True)


def call_with_retry(
    fn: Callable[[], T],
    *,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
    fallback_fn: Callable[[], T] | None = None,
    is_transient_fn: Callable[[BaseException], bool] = is_transient,
) -> T:
    """Run ``fn`` and retry it on transient failures only.

    Transient failures use a short exponential backoff (baked base/cap).
    ``UsageLimitExceeded`` is never retried: if a ``fallback_fn`` is provided
    it is tried exactly once, else the exception re-raises immediately.
    Non-transient errors re-raise immediately; the last error re-raises once
    retries are exhausted.

    *is_transient_fn* lets a provider layer widen the transient set (e.g. the
    OpenRouter upstream-error or DeepSeek reasoning-400 signatures).
    """
    attempts = max(0, constants.TRANSIENT_RETRIES)
    using_fallback = False
    rate_limit_count = 0
    cumulative_backoff = 0.0

    for attempt in range(attempts + 1):
        try:
            if using_fallback:
                assert fallback_fn is not None  # type-narrowing
                return fallback_fn()
            return fn()
        except Exception as e:  # noqa: BLE001 — re-raised unless retryable
            if attempt >= attempts:
                _safe_flush()
                raise

            if is_transient_fn(e):
                delay = min(
                    constants.TRANSIENT_BACKOFF_CAP,
                    constants.TRANSIENT_BACKOFF_BASE * (2**attempt),
                )
                delay += random.uniform(0, delay / 2)  # jitter
                cumulative_backoff += delay
                log.warning(
                    "%s: transient %s (attempt %d/%d) — retrying in %.1fs",
                    what,
                    type(e).__name__,
                    attempt + 1,
                    attempts,
                    delay,
                )
                _safe_flush()
                sleep(delay)
                continue

            if is_rate_limited(e):
                rate_limit_count += 1
                if not using_fallback and fallback_fn is not None:
                    using_fallback = True
                    log.warning(
                        "%s: rate-limit fallback activated on first UsageLimitExceeded",
                        what,
                    )
                    _record_rate_limit_span(
                        count=rate_limit_count,
                        cumulative_backoff=cumulative_backoff,
                        fallback_activated=True,
                    )
                    continue  # try fallback immediately, same attempt slot
                _safe_flush()
                raise

            # non-retryable
            _safe_flush()
            raise
    raise AssertionError("unreachable")  # pragma: no cover


def call_with_retry_and_fallback(
    primary: Callable[[], T],
    fallback: Callable[[], T] | None,
    *,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
    is_transient_primary: Callable[[BaseException], bool] = is_transient,
    is_transient_fallback: Callable[[BaseException], bool] = is_transient,
    should_fallback: Callable[[BaseException], bool] = lambda _e: True,
) -> T:
    """Retry *primary* locally, then fall back to a different model.

    *primary* runs through :func:`call_with_retry` — its own bounded transient
    retries. Only when that whole session fails (retries exhausted, or a
    non-transient/terminal error) AND *fallback* is provided AND
    *should_fallback* of the error is true is *fallback* run, itself through a
    fresh :func:`call_with_retry` session.

    This is "retry locally first, fall back only when local retries failed": the
    fallback model is never tried *instead* of the primary's retries, only
    *after* they are exhausted. *is_transient_primary*/*is_transient_fallback*
    let each model retry on its own provider's transient signatures.
    """
    try:
        return call_with_retry(
            primary, what=what, sleep=sleep, is_transient_fn=is_transient_primary
        )
    except Exception as primary_exc:  # noqa: BLE001 — re-raised when no fallback
        if fallback is None or not should_fallback(primary_exc):
            raise
        log.warning(
            "%s: primary failed after local retries (%s) — falling back to the "
            "secondary model",
            what,
            type(primary_exc).__name__,
        )
        _safe_flush()
        try:
            return call_with_retry(
                fallback,
                what=f"{what} (fallback)",
                sleep=sleep,
                is_transient_fn=is_transient_fallback,
            )
        except Exception as fallback_exc:  # noqa: BLE001
            # Chain the primary cause so the original failure isn't lost when the
            # fallback also fails.
            raise fallback_exc from primary_exc


def _safe_flush() -> None:
    try:
        flush_current_provider()
    except Exception:
        log.warning("trace flush failed", exc_info=True)
