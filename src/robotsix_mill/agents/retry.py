"""Bounded retry+backoff for TRANSIENT model/network failures.

The cheap driver model (tencent/hy3-preview) has a single OpenRouter
provider that intermittently returns HTTP 429 ("Provider returned
error"); transient 5xx / connection blips happen too. These should be
ridden out, not turned into BLOCKED tickets or dropped notifications.

Transient = pydantic-ai ``ModelHTTPError`` with status 429 or 5xx, an
``httpx`` timeout/transport error, or an ``httpx`` 429/5xx response.
Everything else (other 4xx, usage/budget caps, bugs) is NOT retried —
it must surface immediately, unchanged.
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


def call_with_retry(
    fn: Callable[[], T],
    *,
    settings: Settings,
    what: str = "model call",
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn`` and retry it on transient failures with exponential,
    jittered, capped backoff. Re-raises immediately for non-transient
    errors, and re-raises the last error once retries are exhausted."""
    attempts = max(0, settings.transient_retries)
    for attempt in range(attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — re-raised unless transient
            if attempt >= attempts or not is_transient(e):
                raise
            delay = min(
                settings.transient_backoff_cap,
                settings.transient_backoff_base * (2 ** attempt),
            )
            delay += random.uniform(0, delay / 2)  # jitter
            log.warning(
                "%s: transient %s (attempt %d/%d) — retrying in %.1fs",
                what, type(e).__name__, attempt + 1, attempts, delay,
            )
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
