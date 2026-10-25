"""Process-wide GitHub primary rate-limit tracking.

Every GitHub response carries ``x-ratelimit-limit`` /
``x-ratelimit-remaining`` / ``x-ratelimit-reset`` headers describing the
installation (or token) quota.  :func:`record` snapshots them per token so
the fleet can see — without minting a token — how close it is to the
5,000-requests/hour wall, and so callers can back off *before* GitHub starts
answering every request with ``403 Forbidden``:

* non-urgent callers (CI monitor poll, orphaned-pr-check, pin_bump,
  upstream_ci_recovery, stale-branch cleanup) call :func:`should_skip` at the
  top of their pass and skip it (one INFO line naming the reset) when the
  remaining budget has dropped below ``settings.github_rate_limit_reserve``;
* stage callers (merge, ci_fix) do the same but return a same-state re-poll
  instead of hammering ``pr_status`` / ``check_status`` every
  ``merge_poll_seconds``;
* the snapshot is exposed on ``GET /health`` and logged once per hour as
  ``github api budget: used N/limit, reset HH:MM``.

The snapshot is keyed per token (``x-ratelimit-*`` is per installation) but
consumers read the *most constrained* key via :func:`most_constrained`, so
they never need to know which token they are about to spend.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Emit the "github api budget" line at most once per hour per token, and
# throttle a stage caller's skip line to the same cadence, so a tight poll
# loop logs one line an hour rather than one per poll.
_BUDGET_LOG_INTERVAL = 3600.0
_SKIP_LOG_INTERVAL = 3600.0

_lock = threading.Lock()
_snapshots: dict[str, RateLimitSnapshot] = {}
_last_budget_log: dict[str, float] = {}
_last_skip_log: dict[str, float] = {}


@dataclass(frozen=True)
class RateLimitSnapshot:
    """Immutable view of a GitHub rate-limit window for one token."""

    limit: int
    remaining: int
    reset_epoch: int
    observed_epoch: float

    @property
    def reset_at(self) -> datetime:
        """The UTC datetime when the quota window resets."""
        return datetime.fromtimestamp(self.reset_epoch, tz=UTC)

    @property
    def used(self) -> int:
        """Requests consumed in the current window (``limit - remaining``)."""
        return max(0, self.limit - self.remaining)

    def reset_hhmm(self) -> str:
        """The reset time formatted as ``HH:MM`` (UTC) for log/health lines."""
        return self.reset_at.strftime("%H:%M")


def token_key(headers: Mapping[str, str] | None) -> str:
    """Return a stable, non-secret key for the token carried in *headers*.

    Hashes the ``Authorization`` value so the snapshot is keyed per
    installation/token without ever storing the token itself.  Falls back to
    ``"github"`` when no auth header is present.
    """
    if not headers:
        return "github"
    get = getattr(headers, "get", None)
    if not callable(get):
        return "github"
    auth = get("Authorization") or get("authorization") or ""
    if not isinstance(auth, str) or not auth:
        return "github"
    return hashlib.sha256(auth.encode("utf-8")).hexdigest()[:12]


def record(headers: Any, *, key: str | None = None) -> None:
    """Snapshot the GitHub rate-limit headers from a response.

    A no-op unless *headers* carries both ``x-ratelimit-limit`` and
    ``x-ratelimit-remaining`` (GitLab uses differently-named headers, so this
    naturally scopes to GitHub).  Emits the hourly budget line as a
    side-effect, throttled per token.
    """
    get = getattr(headers, "get", None)
    if not callable(get):
        return
    limit_raw = get("x-ratelimit-limit")
    remaining_raw = get("x-ratelimit-remaining")
    if limit_raw is None or remaining_raw is None:
        return
    try:
        limit = int(limit_raw)
        remaining = int(remaining_raw)
        reset_raw = get("x-ratelimit-reset")
        reset_epoch = int(reset_raw) if reset_raw is not None else 0
    except TypeError, ValueError:
        return

    k = key or token_key(headers)
    now = time.time()
    snap = RateLimitSnapshot(
        limit=limit,
        remaining=remaining,
        reset_epoch=reset_epoch,
        observed_epoch=now,
    )
    with _lock:
        _snapshots[k] = snap
        due = (now - _last_budget_log.get(k, 0.0)) >= _BUDGET_LOG_INTERVAL
        if due:
            _last_budget_log[k] = now
    if due:
        logger.info(
            "github api budget: used %d/%d, reset %s",
            snap.used,
            snap.limit,
            snap.reset_hhmm(),
        )


def most_constrained() -> RateLimitSnapshot | None:
    """Return the snapshot with the fewest remaining requests, or ``None``."""
    with _lock:
        snaps = list(_snapshots.values())
    if not snaps:
        return None
    return min(snaps, key=lambda s: s.remaining)


def should_skip(settings: Any, *, now: float | None = None) -> RateLimitSnapshot | None:
    """Return the binding snapshot when a non-urgent caller should back off.

    A caller should skip its pass when the most-constrained token has fewer
    than ``settings.github_rate_limit_reserve`` requests left *and* the window
    has not yet reset.  Returns ``None`` (proceed) when no snapshot exists,
    the reserve is satisfied, or the reset time has already passed.
    """
    snap = most_constrained()
    if snap is None:
        return None
    reserve = int(getattr(settings, "github_rate_limit_reserve", 0) or 0)
    if snap.remaining >= reserve:
        return None
    now = time.time() if now is None else now
    if snap.reset_epoch <= now:
        return None
    return snap


def log_skip(
    log: logging.Logger,
    context: str,
    snap: RateLimitSnapshot,
    *,
    now: float | None = None,
) -> None:
    """Log a throttled (once-per-hour per *context*) rate-limit skip line.

    Use from a tight poll loop (merge / ci_fix) so a rate-limited window
    produces one INFO line an hour rather than one per poll.
    """
    now = time.time() if now is None else now
    with _lock:
        if (now - _last_skip_log.get(context, 0.0)) < _SKIP_LOG_INTERVAL:
            return
        _last_skip_log[context] = now
    log.info(
        "%s: GitHub rate-limit reserve reached (%d/%d remaining) — "
        "waiting for reset %s instead of polling",
        context,
        snap.remaining,
        snap.limit,
        snap.reset_hhmm(),
    )


def health_payload() -> dict[str, Any]:
    """Return a JSON-serialisable view of the most-constrained window.

    Empty dict when nothing has been observed yet.
    """
    snap = most_constrained()
    if snap is None:
        return {}
    return {
        "limit": snap.limit,
        "remaining": snap.remaining,
        "used": snap.used,
        "reset": snap.reset_at.isoformat(),
        "reset_hhmm": snap.reset_hhmm(),
    }


def _reset_for_tests() -> None:
    """Clear all process-wide snapshot state (test fixtures only)."""
    with _lock:
        _snapshots.clear()
        _last_budget_log.clear()
        _last_skip_log.clear()
