"""Classification of stage-level exceptions into transient vs fatal.

Separate from ``agents/retry.py``, which handles LLM-call-level
retries. This module classifies errors at the stage-runner level.
"""

from __future__ import annotations

import re
import subprocess

import httpx

try:
    import openai
except ImportError:  # pragma: no cover
    openai = None  # type: ignore[no-redef]

_TRANSIENT_HTTPX_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.TimeoutException,
    httpx.TransportError,
)

_GIT_TRANSIENT_RE = re.compile(
    r"(Internal Server Error|500|503|HTTP/.* 5\d\d|Connection refused)"
)
_GIT_FATAL_TRANSIENT_RE = re.compile(
    r"(remote rejected.*[Ii]nternal [Ss]erver|fatal: unable to access)"
)

_MAX_CHAIN_WALK = 10


def _is_transient_httpx(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT_HTTPX_EXCEPTIONS):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _is_transient_openai(exc: BaseException) -> bool:
    if openai is None:
        return False
    return isinstance(
        exc,
        (
            openai.APIConnectionError,
            openai.RateLimitError,
            openai.APITimeoutError,
            openai.InternalServerError,
        ),
    )


def _is_transient_called_process_error(exc: BaseException) -> bool:
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    stderr = exc.stderr
    if stderr is None:
        return False
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return bool(
        _GIT_TRANSIENT_RE.search(stderr) or _GIT_FATAL_TRANSIENT_RE.search(stderr)
    )


def classify_stage_error(exc: BaseException) -> str:
    """Return ``"transient"`` or ``"fatal"`` for a stage exception.

    Walks ``__cause__`` / ``__context__`` up to *MAX_CHAIN_WALK*
    levels.  Any matching transient pattern anywhere in the chain
    makes the whole error transient.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN_WALK):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))

        if _is_transient_httpx(current):
            return "transient"
        if _is_transient_openai(current):
            return "transient"
        if _is_transient_called_process_error(current):
            return "transient"

        if current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
        elif current.__context__ is not None and id(current.__context__) not in seen:
            current = current.__context__
        else:
            break

    return "fatal"
