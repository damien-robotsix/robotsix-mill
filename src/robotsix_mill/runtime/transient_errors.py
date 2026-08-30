"""Classification of stage-level exceptions into transient vs fatal.

Separate from ``agents/retry.py``, which handles LLM-call-level
retries. This module classifies errors at the stage-runner level.
"""

from __future__ import annotations

import errno
import os
import re
import socket
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
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

# Disk-exhaustion signatures. Matched against git/subprocess stderr and
# exception text anywhere in the cause chain, for the cases where ENOSPC
# reaches us as text rather than as an OSError errno: a ``git clone``
# that died with "No space left on device" surfaces as a
# CalledProcessError, whose "Fatal: CalledProcessError" block note gives
# no hint that the disk — not the repo — was the problem.
_DISK_FULL_RE = re.compile(
    r"(No space left on device"
    r"|Disk quota exceeded"
    r"|ENOSPC"
    r"|not enough space"
    r"|cannot write.*database or disk is full"
    r"|database or disk is full)",
    re.IGNORECASE,
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

# LLM-provider model-outage signatures: a specific model or the whole
# provider is returning 503 / overloaded / unavailable. Distinct from a
# generic upstream 5xx (which bounded retries handle) — this is a
# persistent condition where every stage touching that model fails
# identically, so the worker parks rather than burning the retry budget.
# Matched against httpx response bodies and exception text anywhere in
# the cause chain.
_MODEL_UNAVAILABLE_RE = re.compile(
    r"(model\b.*\b(?:unavailable|not available|currently unavailable)"
    r"|no healthy endpoint"
    r"|no available endpoint"
    r"|all endpoints unavailable"
    r"|provider\b.*\boverloaded"
    r"|overloaded.*try again"
    r"|the model .* is currently at capacity"
    # llmio's tier router: the requested tier is cooling down after
    # provider failures and every fallback tier is exhausted too, so no
    # model can serve the call right now. Same shape as an outage — the
    # cooldown lifts by itself. Seen 2026-08-29 as "scope triage: level2
    # is in cooldown and fallback depth (2) exhausted".
    r"|is in cooldown and fallback depth)",
    re.IGNORECASE,
)

# Message-string fallback patterns for transient errors not caught by
# exception-type checks.  These match against ``str(exc)`` anywhere in
# the cause chain when no type-based classifier fires.
_TRANSIENT_MESSAGE_RE = [
    re.compile(r"[Ii]nvalid response from openrouter"),
    re.compile(r"[Ee]xceeded max(imum)? output retries"),
    # Lock contention on the mill's own per-board SQLite DB: a write
    # burst can outlast the connection's busy timeout and the resulting
    # OperationalError says nothing about the ticket's work — the same
    # stage succeeds once the writer that held the lock finishes.
    # Classifying it fatal turned internal DB contention into BLOCKED
    # tickets needing a manual resume.
    re.compile(r"database is locked"),
]


# LLM provider / model-configuration failures that are NOT a property of
# the ticket's spec: the model never produced an answer, so the attempt
# says nothing about whether the spec is implementable.  They are still
# blocking (a bad baked model id does not fix itself between retries), but
# the implement stage must not record a spec fingerprint for them — that
# fingerprint turns the next resume into a free "spec unchanged" re-block,
# pinning the ticket until a human edits the description.  Seen live on
# 2026-08-25/27 across 10 tickets when llmio shipped an unroutable
# level-1 slug and a level-2 cap smaller than its own reasoning budget.
# Claude subscription quota exhaustion. llmio raises
# ``ClaudeSDKUsageExhaustedError`` carrying the assistant-visible text
# ("You've hit your session limit · resets 9:20am (UTC)", "You're out of
# usage credits", "You've hit your limit · resets 8pm (UTC)"). The quota
# comes back by itself at the stated reset, so this is a PARK (model-outage
# shaped: infrastructure, retry budget untouched), never a BLOCK — and,
# per the operator's cost preference, never a silent fallback onto a paid
# OpenRouter tier unless ``claude_exhaustion_paid_fallback`` is on. Seen
# 2026-08-29: 6 tickets BLOCKED "agent error — resumable: You've hit your
# session limit" needing a manual resume after the window reset.
_CLAUDE_USAGE_EXHAUSTED_RE = re.compile(
    r"(out of usage credits"
    r"|hit your session limit"
    r"|hit your limit"
    r"|usage limit reached)",
    re.IGNORECASE,
)
_CLAUDE_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(?UTC\)?",
    re.IGNORECASE,
)


def _matches_claude_usage_exhausted(exc: BaseException) -> bool:
    if type(exc).__name__ == "ClaudeSDKUsageExhaustedError":
        return True
    return bool(_CLAUDE_USAGE_EXHAUSTED_RE.search(str(exc)))


def _walk_chain(exc: BaseException) -> list[BaseException]:
    seen: set[int] = set()
    out: list[BaseException] = []
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN_WALK):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        out.append(current)
        if current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
        elif current.__context__ is not None and id(current.__context__) not in seen:
            current = current.__context__
        else:
            break
    return out


def is_claude_usage_exhausted(exc: BaseException) -> bool:
    """True when *exc* (or its cause chain) is a Claude subscription quota
    exhaustion — a per-provider condition that clears at the stated reset.
    """
    return any(_matches_claude_usage_exhausted(c) for c in _walk_chain(exc))


def claude_usage_reset_at(
    exc: BaseException, *, now: datetime | None = None
) -> datetime | None:
    """Parse the ``resets <time> (UTC)`` hint out of a usage-exhaustion
    message into the next such instant (UTC), or None when absent.
    """
    for c in _walk_chain(exc):
        m = _CLAUDE_RESET_RE.search(str(c))
        if not m:
            continue
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        base = now or datetime.now(UTC)
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate
    return None


_PROVIDER_FAILURE_RE = re.compile(
    r"(is not a valid model ID"
    r"|not a valid model"
    r"|[Mm]odel token limit \(\d+\) exceeded"
    r"|exceeded before any response was generated"
    r"|output retries exhausted"
    r"|[Ee]xceeded max(imum)? output retries"
    r"|[Ii]nvalid response from openrouter"
    r"|finish_reason=.error"
    r"|status_code: 4\d\d, model_name:)",
)


def _matches_provider_failure(exc: BaseException) -> bool:
    return bool(
        _PROVIDER_FAILURE_RE.search(str(exc))
    ) or _matches_claude_usage_exhausted(exc)


def is_provider_failure(exc: BaseException) -> bool:
    """True when *exc* (or its cause chain) is an LLM provider/model failure.

    Distinct from :func:`classify_stage_error`'s "transient": a transient
    error is retried by the worker; a provider failure may well be
    permanent for this deployment (wrong model id, cap below the reasoning
    budget) and blocks — but it is never *spec-determined*, so callers
    must not persist a spec fingerprint for it.  Walks the cause chain
    like :func:`classify_stage_error`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN_WALK):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if _matches_provider_failure(current):
            return True
        if current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
        elif current.__context__ is not None and id(current.__context__) not in seen:
            current = current.__context__
        else:
            break
    return False


def _is_transient_message(exc: BaseException) -> bool:
    """Return True when *exc*'s string representation matches a
    known transient-error pattern not covered by type checks.
    """
    msg = str(exc)
    return any(pattern.search(msg) for pattern in _TRANSIENT_MESSAGE_RE)


_MAX_CHAIN_WALK = 10


# GitHub answers a throttled caller with 403 (primary quota exhausted or
# secondary/abuse rate limit) or 429, not 5xx — so the plain
# ``5xx == transient`` rule below classified throttling as FATAL and every
# throttled forge call became a BLOCKED ticket needing a manual resume.
# Observed on 2026-08-11/12: three robotsix-file-hub tickets blocked with a
# misleading "PR create failed: 422 … a pull request already exists" (the
# existing-PR lookup that would have recovered the URL was itself throttled),
# and a robotsix-chat-mobile ticket blocked outright with
# "Fatal: HTTPStatusError: Client error '403 Forbidden' for url
# '…/pulls?head=…'".  Nothing about the ticket's work changed; the same call
# succeeds once the window resets.
_GITHUB_RATE_LIMIT_BODY_RE = re.compile(
    r"(rate limit|abuse detection|secondary rate)", re.IGNORECASE
)


def _is_github_rate_limited(response: Any) -> bool:
    """Return True when *response* is a GitHub throttle, not a real refusal.

    Requires an explicit throttle signal on a 403/429 — an exhausted
    ``x-ratelimit-remaining``, a ``retry-after`` header, or a rate-limit
    phrase in the body.  A bare 403 stays fatal so a genuine permission
    failure (App not installed on the repo, token missing a scope) still
    blocks instead of retrying forever; a bare 429 keeps its existing
    fatal classification.

    Header and body values are only trusted when they are the ``str``/``int``
    the HTTP layer actually produces, so a response object that cannot
    answer these questions is treated as "no signal" rather than a throttle.
    """
    if getattr(response, "status_code", None) not in (403, 429):
        return False

    headers = getattr(response, "headers", None)
    get = getattr(headers, "get", None)
    if callable(get):
        remaining = get("x-ratelimit-remaining")
        if isinstance(remaining, (str, int)) and str(remaining).strip() == "0":
            return True
        if isinstance(get("retry-after"), (str, int)):
            return True

    body = getattr(response, "text", None)
    return bool(isinstance(body, str) and _GITHUB_RATE_LIMIT_BODY_RE.search(body))


def _is_transient_httpx(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT_HTTPX_EXCEPTIONS):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        if 500 <= exc.response.status_code < 600:
            return True
        return _is_github_rate_limited(exc.response)
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


def _matches_model_unavailable(exc: BaseException) -> bool:
    """Return True when *exc* looks like an LLM provider reporting
    "model unavailable", "overloaded", or similar outage signal.

    Checks the response body/JSON on a 503, and the string
    representation anywhere else. A 503 with no body match is left to
    the generic transient classifiers (bounded retries); this function
    only fires when the provider explicitly signals a model outage.
    """
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 503:
        try:
            body = exc.response.text
        except Exception:
            body = ""
        if body and _MODEL_UNAVAILABLE_RE.search(body):
            return True
        # Also check JSON error field (OpenRouter-style).
        try:
            js = exc.response.json()
            err = str(js.get("error", {}).get("message", ""))
            if _MODEL_UNAVAILABLE_RE.search(err):
                return True
        except Exception:
            pass
    if openai is not None and isinstance(
        exc,
        (openai.InternalServerError, openai.APIConnectionError),
    ):
        return bool(_MODEL_UNAVAILABLE_RE.search(str(exc)))
    return bool(_MODEL_UNAVAILABLE_RE.search(str(exc)))


def _matches_disk_full(exc: BaseException) -> bool:
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return True
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = exc.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        if stderr and _DISK_FULL_RE.search(stderr):
            return True
    return bool(_DISK_FULL_RE.search(str(exc)))


def is_disk_full_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like the data volume running out.

    The disk analogue of :func:`is_network_down_error`, and paired with
    :func:`disk_space_available` the same way. A full volume is not a
    property of the ticket that happened to hit it: every board fails
    identically until space is freed, so the worker parks rather than
    spending the retry budget and then blocking FATALLY. Walks the cause
    chain like :func:`classify_stage_error`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN_WALK):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if _matches_disk_full(current):
            return True
        if current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
        elif current.__context__ is not None and id(current.__context__) not in seen:
            current = current.__context__
        else:
            break
    return False


def disk_space_available(path: str | Path, min_free_mb: int) -> bool:
    """True when *path*'s filesystem has at least *min_free_mb* free.

    Uncached, unlike :func:`network_available`: ``statvfs`` is a single
    cheap syscall against the local filesystem, and staleness here has
    teeth in both directions — a cached "full" would keep parking
    tickets after the GC freed space, and a cached "fine" would wave
    them into a volume that just filled.

    A floor of 0 disables the check. Returns True when the path cannot
    be stat'ed, so an unreadable mount degrades to the pre-existing
    behaviour rather than parking the whole fleet.
    """
    if min_free_mb <= 0:
        return True
    try:
        st = os.statvfs(str(path))
    except OSError:
        return True
    return st.f_bavail * st.f_frsize >= min_free_mb * 1024 * 1024


def first_full_path(paths: Sequence[str | Path], min_free_mb: int) -> str | None:
    """The first of *paths* below *min_free_mb* free, or ``None`` if all are OK.

    A stage needs room on more than one filesystem, and checking only the data
    volume misses the one that actually fills. Sandbox containers write their
    package installs to the Docker overlay, which lives on the host root
    filesystem — a different device from the workspace volume. On 2026-08-07 a
    rebase failed three times with "No space left on device" on *every*
    ``run_command`` (even ``echo hello``) while the data volume reported 146 GB
    free: root was at 80%, and the gate, looking only at the data volume, waved
    the ticket straight into the wall it had just hit.

    Returns the offending path so the caller can name it in the park note —
    "which disk" is the first thing an operator needs and the hardest thing to
    reconstruct after the fact.
    """
    for p in paths:
        if not disk_space_available(p, min_free_mb):
            return str(p)
    return None


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


# Stable marker constant so the run-health digest and notification
# system can recognise model-outage events distinctly from work blockers.
MODEL_OUTAGE_MARKER: str = "model_outage"


def is_model_unavailable_error(exc: BaseException) -> bool:
    """Return True when *exc* looks like an LLM provider model outage.

    Distinct from plain "transient": a 503 with no body match is
    upstream endpoint trouble worth bounded retries, but a "model
    unavailable" / "overloaded" / "no healthy endpoint" signal means
    every stage touching that model is about to fail the same way. The
    worker pairs this with a park branch (mirroring the network/disk
    parks) so the ticket re-polls without consuming its retry budget.

    Walks the cause chain like :func:`classify_stage_error`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN_WALK):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if _matches_model_unavailable(current) or _matches_claude_usage_exhausted(
            current
        ):
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
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 402:
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
        except Exception:
            pass  # best-effort JSON parse; if it fails, fall through to string check below
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
        except Exception:
            pass  # best-effort text body read; non-text responses fall through
        try:
            js = exc.response.json()
            err = str(js.get("error", {}).get("message", ""))
            if err:
                msg = err
        except Exception:
            pass  # best-effort JSON body read; non-JSON responses fall through

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
    model blip (OpenRouter 5xx/429/timeout, provider-specific reasoning
    errors) into a block needing a manual resume.

    Call this at the top of such an except-clause: a transient error is
    re-raised so the worker's ``classify_stage_error`` schedules a fresh
    re-run with backoff (bounded by ``stage_retry_max_attempts``); a
    fatal error returns and the caller blocks as before. This is the
    same fix applied inline in ``stages/implement.py``, factored out so
    the LLM stages stay consistent.
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
    # A full disk is infrastructure, not a defect in the ticket: nothing
    # about the work changed, and the same stage succeeds once space is
    # freed. Classifying it fatal is what turned one full volume into 146
    # hand-resumable blocks on 2026-08-06. Reaching "transient" here is
    # also what lets the disk-full PARK in the worker fire at all — that
    # branch lives inside the transient path.
    if _matches_disk_full(exc):
        return True
    # A model-unavailable / provider-overloaded signal is infrastructure,
    # not a defect: the same stage succeeds once the model recovers.
    # Reaching "transient" here is what lets the model-outage PARK in the
    # worker fire — that branch lives inside the transient path.
    if _matches_model_unavailable(exc):
        return True
    # Claude quota exhaustion is the same shape: the tier is out until the
    # quota resets, nothing about the ticket changed. "transient" here is
    # what routes it into the worker's model-outage PARK.
    if _matches_claude_usage_exhausted(exc):
        return True
    return bool(_is_transient_message(exc))


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
        # NOTE: the provider-specific thinking-mode reasoning round-trip
        # 400 detector was removed — the upstream provider no longer raises
        # that 400 when reasoning is stripped from a tool-call turn, so
        # robotsix-llmio dropped the detector and this classifier branch
        # with it. A plain 400 is fatal.

        if current.__cause__ is not None and id(current.__cause__) not in seen:
            current = current.__cause__
        elif current.__context__ is not None and id(current.__context__) not in seen:
            current = current.__context__
        else:
            break

    return "fatal"
