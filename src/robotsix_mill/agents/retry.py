"""Bounded retry+backoff for TRANSIENT model/network failures and
rate-limit (UsageLimitExceeded) errors.

The cheap driver model (tencent/hy3-preview) has a single OpenRouter
provider that intermittently returns HTTP 429 ("Provider returned
error"); transient 5xx / connection blips happen too. These should be
ridden out, not turned into BLOCKED tickets or dropped notifications.

Transient = pydantic-ai ``ModelHTTPError`` with status 429 or 5xx, an
``httpx`` timeout/transport error, or an ``httpx`` 429/5xx response.

``UsageLimitExceeded`` (pydantic-ai budget cap) is transient-like but
uses a longer, rate-limit-aware backoff schedule (30s base, 120s cap)
and supports model/provider fallback after consecutive failures.

Everything else (other 4xx, bugs) is NOT retried — it must surface
immediately, unchanged.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

from ..config import Settings

log = logging.getLogger("robotsix_mill.retry")

T = TypeVar("T")


def _status(exc: BaseException) -> int | None:
    # pydantic-ai ModelHTTPError(status_code, ...) and httpx
    # HTTPStatusError(response.status_code) both expose a status.
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    rc = getattr(resp, "status_code", None)
    return rc if isinstance(rc, int) else None


# JSONDecodeError: the model occasionally emits malformed JSON for a
# tool call / structured output; a re-run almost always yields valid
# JSON, so treat it as transient instead of hard-ERRORing the ticket.
_TRANSIENT_NAMES = {
    "APITimeoutError", "APIConnectionError", "JSONDecodeError",
}


def is_transient(exc: BaseException) -> bool:
    """True only for retryable infrastructure failures. Walks the
    cause/context chain so a timeout wrapped by openai/pydantic-ai
    (e.g. ModelHTTPError <- APITimeoutError <- httpx.ReadTimeout) is
    still recognised."""
    import httpx

    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 10:
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
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


def is_rate_limited(exc: BaseException) -> bool:
    """True only for ``UsageLimitExceeded`` (the pydantic-ai budget-cap
    exception).  Walks the cause/context chain so a wrapped exception is
    still recognised.

    This is distinct from :func:`is_transient` — ``UsageLimitExceeded``
    is NOT transient (re-running immediately just re-hits the limit),
    but it IS rate-limited (retryable with a longer, rate-limit-aware
    backoff and optional model/provider fallback).
    """
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 10:
        if type(cur).__name__ == "UsageLimitExceeded":
            return True
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return False


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Walk the exception chain looking for retry-after information.

    Checks for a ``retry_after_seconds`` attribute or an ``httpx.Response``
    with a ``Retry-After`` header.  Returns ``None`` if no such information
    is found — the caller falls back to the computed backoff.
    """
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 10:
        # Direct retry_after_seconds attribute (forward-looking)
        ra = getattr(cur, "retry_after_seconds", None)
        if isinstance(ra, (int, float)):
            return float(ra)
        # httpx.Response with Retry-After header
        resp = getattr(cur, "response", None)
        if resp is not None:
            headers = getattr(resp, "headers", None)
            if headers is not None:
                ra_val = headers.get("Retry-After") or headers.get("retry-after")
                if ra_val is not None:
                    try:
                        return float(ra_val)
                    except (TypeError, ValueError):
                        pass
        cur = cur.__cause__ or cur.__context__
        seen += 1
    return None


def _record_rate_limit_span(
    delay: float,
    cumulative_backoff: float,
    count: int,
    fallback_activated: bool,
    fallback_model: str,
) -> None:
    """Record rate-limit backoff metrics on the current OTel span.

    No-op when OpenTelemetry is not installed or no span is recording.
    """
    try:
        from opentelemetry import trace as otel_trace  # type: ignore[import-untyped]
    except ImportError:
        return
    span = otel_trace.get_current_span()
    if span is None or not span.is_recording():
        return
    span.set_attribute("mill.rate_limit.count", count)
    span.set_attribute("mill.rate_limit.backoff_seconds", cumulative_backoff)
    if fallback_activated:
        span.set_attribute("mill.rate_limit.fallback_activated", True)
        span.set_attribute("mill.rate_limit.fallback_model", fallback_model)


def call_with_retry(
    fn: Callable[[], T],
    *,
    settings: Settings,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
    fallback_fn: Callable[[], T] | None = None,
) -> T:
    """Run ``fn`` and retry it on transient / rate-limit failures.

    Transient failures (429, 5xx, timeouts, JSON-decode) use a short
    exponential backoff (2s base, 30s cap).  ``UsageLimitExceeded``
    (budget cap) uses a longer, rate-limit-aware backoff (30s base,
    120s cap) and, when ``fallback_fn`` is provided, switches to calling
    it after ``settings.rate_limit_fallback_retries`` consecutive
    rate-limit failures.

    Re-raises immediately for non-transient, non-rate-limited errors,
    and re-raises the last error once retries are exhausted.
    """
    from ..runtime import tracing  # lazy import

    attempts = max(0, settings.transient_retries)
    consecutive_rate_limits = 0
    using_fallback = False
    rate_limit_count = 0
    cumulative_backoff = 0.0

    for attempt in range(attempts + 1):
        try:
            if using_fallback:
                assert fallback_fn is not None  # type-narrowing
                return fallback_fn()
            result = fn()
            return result
        except Exception as e:  # noqa: BLE001 — re-raised unless retryable
            if attempt >= attempts:
                try:
                    tracing.flush_tracing()
                except Exception:
                    log.warning("flush_tracing failed", exc_info=True)
                raise

            # --- transient branch (unchanged) ---------------------------------
            if is_transient(e):
                delay = min(
                    settings.transient_backoff_cap,
                    settings.transient_backoff_base * (2 ** attempt),
                )
                delay += random.uniform(0, delay / 2)  # jitter
                log.warning(
                    "%s: transient %s (attempt %d/%d) — retrying in %.1fs",
                    what, type(e).__name__, attempt + 1, attempts, delay,
                )
                try:
                    tracing.flush_tracing()
                except Exception:
                    log.warning("flush_tracing failed", exc_info=True)
                sleep(delay)
                continue

            # --- rate-limit branch (new) --------------------------------------
            if is_rate_limited(e):
                consecutive_rate_limits += 1
                rate_limit_count += 1

                # Fallback activation on threshold
                if (
                    not using_fallback
                    and fallback_fn is not None
                    and consecutive_rate_limits
                    >= settings.rate_limit_fallback_retries
                ):
                    using_fallback = True
                    fallback_model = settings.rate_limit_fallback_model
                    log.warning(
                        "%s: rate-limit fallback activated after %d "
                        "consecutive UsageLimitExceeded failures "
                        "(model=%s)",
                        what, consecutive_rate_limits, fallback_model,
                    )
                    _record_rate_limit_span(
                        delay=0.0,
                        cumulative_backoff=cumulative_backoff,
                        count=rate_limit_count,
                        fallback_activated=True,
                        fallback_model=fallback_model,
                    )
                    # Try fallback immediately — same attempt slot
                    continue

                # Compute rate-limit backoff delay
                delay = min(
                    settings.rate_limit_backoff_cap,
                    settings.rate_limit_backoff_base * (2 ** attempt),
                )
                # Honor Retry-After if present in the exception chain
                retry_after = _retry_after_seconds(e)
                if retry_after is not None:
                    delay = max(delay, retry_after)
                delay += random.uniform(0, delay / 2)  # jitter

                cumulative_backoff += delay
                _record_rate_limit_span(
                    delay=delay,
                    cumulative_backoff=cumulative_backoff,
                    count=rate_limit_count,
                    fallback_activated=using_fallback,
                    fallback_model=settings.rate_limit_fallback_model,
                )
                log.warning(
                    "%s: rate-limited %s (attempt %d/%d) — retrying in %.1fs",
                    what, type(e).__name__, attempt + 1, attempts, delay,
                )
                try:
                    tracing.flush_tracing()
                except Exception:
                    log.warning("flush_tracing failed", exc_info=True)
                sleep(delay)
                continue

            # --- non-retryable -------------------------------------------------
            try:
                tracing.flush_tracing()
            except Exception:
                log.warning("flush_tracing failed", exc_info=True)
            raise
    raise AssertionError("unreachable")  # pragma: no cover
