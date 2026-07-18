"""Classification of stage-level exceptions into transient vs fatal.

Separate from ``agents/retry.py``, which handles LLM-call-level
retries. This module classifies errors at the stage-runner level.
"""

from __future__ import annotations

import re
import socket
import subprocess
import time
from typing import Any

import httpx

from robotsix_mill.sandbox import SandboxError

openai: Any = None
try:
    import openai as _openai

    openai = _openai
except ImportError:  # pragma: no cover
    pass

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
    r"(remote rejected.*[Ii]nternal [Ss]erver|fatal: unable to access|fatal: Authentication failed)"
)
# A reclaimed/missing workspace clone: the per-ticket clone dir vanished
# mid-run (orphan-workspace reclaim, disk cleanup, …), so a ``git -C <dir> …``
# fails with "not a git repository" or "cannot change to <dir>: No such file".
# Treat it as transient so the worker RETRIES the stage instead of emitting a
# cryptic "Fatal: CalledProcessError" block: implement's clone-or-resume
# (_clone_and_branch) re-clones a fresh workspace on the retry, so it
# self-heals with no manual resume.
_GIT_WORKSPACE_GONE_RE = re.compile(
    r"(fatal: not a git repository"
    r"|fatal: cannot change to .*No such file or directory)"
)

# Host-resolution failure signatures: the network (or its DNS) is gone,
# not just one endpoint hiccuping. Matched against git stderr and
# exception text anywhere in the cause chain.
_NETWORK_DOWN_RE = re.compile(
    r"(Could not resolve host"
    r"|Temporary failure in name resolution"
    r"|Name or service not known"
    r"|getaddrinfo failed"
    r"|Network is unreachable)"
)

# Message-string fallback patterns for transient errors not caught by
# exception-type checks.  These match against ``str(exc)`` anywhere in
# the cause chain when no type-based classifier fires.
_TRANSIENT_MESSAGE_RE = [
    re.compile(r"[Ii]nvalid response from openrouter"),
    re.compile(r"[Ee]xceeded max(imum)? output retries"),
]


def _is_transient_message(exc: BaseException) -> bool:
    """Return True when *exc*'s string representation matches a
    known transient-error pattern not covered by type checks."""
    msg = str(exc)
    return any(pattern.search(msg) for pattern in _TRANSIENT_MESSAGE_RE)


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
        _GIT_TRANSIENT_RE.search(stderr)
        or _GIT_FATAL_TRANSIENT_RE.search(stderr)
        or _GIT_WORKSPACE_GONE_RE.search(stderr)
    )


def _matches_network_down(exc: BaseException) -> bool:
    if isinstance(exc, socket.gaierror):
        return True
    if isinstance(exc, httpx.ConnectError):
        return True
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if stderr and _NETWORK_DOWN_RE.search(stderr):
            return True
    return bool(_NETWORK_DOWN_RE.search(str(exc)))


def is_network_down_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like a GLOBAL network/DNS outage.

    Distinct from plain "transient": a 503 from one forge is endpoint
    trouble worth bounded retries, but a host-resolution failure means
    every network-touching stage on every board is about to fail the
    same way. The worker pairs this with :func:`network_available` to
    park tickets without consuming their retry budget — otherwise an
    outage longer than the ~1-minute retry envelope mass-blocks the
    whole board. Walks the cause chain like :func:`classify_stage_error`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN_WALK):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if _matches_network_down(current):
            return True
        if current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
        elif current.__context__ is not None and id(current.__context__) not in seen:
            current = current.__context__
        else:
            break
    return False


# Cached connectivity probe — every concurrently-failing ticket asks the
# same question within seconds of each other. ``at`` starts at -inf so
# the FIRST call always probes: time.monotonic() is seconds-since-boot
# on Linux, so a small sentinel like 0.0 would read as "fresh cache"
# during the first cache window after boot.
_probe_cache: dict[str, float | bool] = {"at": float("-inf"), "ok": True}


def network_available(host: str, *, cache_seconds: float = 30.0) -> bool:
    """Cheap cached check that *host* resolves (DNS reachability).

    Resolution is the cheapest end-to-end signal for "is the network
    there at all" and matches the failure mode that motivates the check
    (``Could not resolve host``). Results are cached *cache_seconds* so
    a burst of failing tickets costs one lookup.
    """
    now = time.monotonic()
    if now - float(_probe_cache["at"]) < cache_seconds:
        return bool(_probe_cache["ok"])
    try:
        socket.getaddrinfo(host, 443)
        ok = True
    except OSError:
        ok = False
    _probe_cache["at"] = now
    _probe_cache["ok"] = ok
    return ok


# --- OpenRouter 402 insufficient-credit detection ---------------------------

_INSUFFICIENT_CREDIT_RE = re.compile(
    r"(insufficient_credits"
    r"|requires more credits"
    r"|Insufficient credits"
    r"|insufficient.*balance"
    r"|credit.*balance.*insufficient"
    r"|You need to add more credits)",
    re.IGNORECASE,
)

_SHORTFALL_RE = re.compile(
    r"(?:can only afford\s+)(\d+)|"
    r"(?:requested up to\s+)(\d+)\s+tokens.*?(?:can only afford\s+)(\d+)",
    re.IGNORECASE,
)


def _matches_insufficient_credit(exc: BaseException) -> bool:
    """Return True when *exc* looks like an OpenRouter 402 credit-shortfall."""
    msg = str(exc)
    if _INSUFFICIENT_CREDIT_RE.search(msg):
        return True
    # httpx.HTTPStatusError: response body may contain the message
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 402:
            try:
                body = exc.response.text
            except Exception:
                body = ""
            if _INSUFFICIENT_CREDIT_RE.search(body):
                return True
            # Also check JSON error field
            try:
                js = exc.response.json()
                err = str(js.get("error", {}).get("message", ""))
                if _INSUFFICIENT_CREDIT_RE.search(err):
                    return True
            except Exception:  # noqa: S110 -- defensive parse, ignore
                pass
    # openai.PermissionDeniedError (402)
    if openai is not None and isinstance(exc, openai.PermissionDeniedError):
        # PermissionDeniedError doesn't expose http_status directly;
        # detect 402 via the string message.
        if "402" in str(exc) or "insufficient" in str(exc).lower():
            return True
    return False


def _check_one_insufficient_credit(exc: BaseException) -> bool:
    """Check a single exception node (not the chain)."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 402:
        if _matches_insufficient_credit(exc):
            return True
    if openai is not None and isinstance(exc, openai.PermissionDeniedError):
        return _matches_insufficient_credit(exc)
    return _matches_insufficient_credit(exc)


def is_insufficient_credit(exc: BaseException) -> bool:
    """Return True when *exc* (or any node in its cause chain) is an
    OpenRouter 402 insufficient-credit error.

    Walks ``__cause__`` / ``__context__`` up to ``_MAX_CHAIN_WALK``
    levels — same pattern as :func:`classify_stage_error`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN_WALK):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if _check_one_insufficient_credit(current):
            return True
        if current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
        elif current.__context__ is not None and id(current.__context__) not in seen:
            current = current.__context__
        else:
            break
    return False


def parse_credit_shortfall(exc: BaseException) -> str:
    """Extract a human-readable shortfall message from a 402 error.

    Returns ``""`` when no shortfall numbers can be parsed.
    """
    msg = str(exc)
    # Try JSON body first
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            body = exc.response.text
            if body:
                msg = body
        except Exception:  # noqa: S110 -- defensive parse, ignore
            pass
        try:
            js = exc.response.json()
            err = str(js.get("error", {}).get("message", ""))
            if err:
                msg = err
        except Exception:  # noqa: S110 -- defensive parse, ignore
            pass

    m = _SHORTFALL_RE.search(msg)
    if m is None:
        return ""
    if m.group(1):
        return f"can only afford {m.group(1)} tokens"
    if m.group(2) and m.group(3):
        return f"requested up to {m.group(2)} tokens, can only afford {m.group(3)}"
    return ""


def reraise_if_transient(exc: BaseException) -> None:
    """Re-raise *exc* when it's a transient stage error, else return.

    LLM-agent stages (review, refine, retrospect) historically caught
    every exception and converted it to a hard ``BLOCKED`` Outcome —
    which BYPASSES the worker's stage-retry. That turned every transient
    model blip (OpenRouter 5xx/429/timeout, the DeepSeek thinking-mode
    reasoning round-trip 400) into a block needing a manual resume.

    Call this at the top of such an except-clause: a transient error is
    re-raised so the worker's ``classify_stage_error`` schedules a fresh
    re-run with backoff (bounded by ``stage_retry_max_attempts``); a
    fatal error returns and the caller blocks as before. This is the
    same fix applied inline in ``stages/implement.py``, factored out so
    the LLM stages stay consistent. See [[project-deepseek-pin-reasoning-blocker]].
    """
    if classify_stage_error(exc) == "transient":
        raise exc


def _check_one_transient(exc: BaseException) -> bool:
    """Return True when *exc* matches any known transient-error classifier."""
    if _is_transient_httpx(exc):
        return True
    if _is_transient_openai(exc):
        return True
    if _is_transient_called_process_error(exc):
        return True
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, SandboxError):
        return True
    if _is_transient_message(exc):
        return True
    return False


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

        if _check_one_transient(current):
            return "transient"
        # NOTE: the DeepSeek thinking-mode reasoning round-trip 400 detector
        # was removed — OpenRouter no longer raises that 400 when reasoning is
        # stripped from a tool-call turn, so robotsix-llmio dropped the
        # detector and this classifier branch with it. A plain 400 is fatal.

        if current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
        elif current.__context__ is not None and id(current.__context__) not in seen:
            current = current.__context__
        else:
            break

    return "fatal"
