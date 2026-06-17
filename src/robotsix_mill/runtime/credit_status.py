"""Thread-safe, module-level low-credit warning state for the board UI.

Mirrors the ``tracing.py`` export-failure pattern: a ``_state`` dict
guarded by ``threading.Lock()``, updated by both the reactive 402
detection path and the proactive balance-poll runner, and read by the
``GET /credit-status`` API route.  No persistence — the warning resets
on restart.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any


_state: dict[str, Any] = {}
_lock = threading.Lock()

# Keys stored in _state.  Every key always exists after the first write;
# None / 0.0 mean "no data / healthy".
_KEY_LOW = "low"
_KEY_BALANCE = "balance_usd"
_KEY_THRESHOLD = "threshold_usd"
_KEY_LAST_CHECKED = "last_checked"
_KEY_LAST_402_AT = "last_402_at"
_KEY_LAST_402_DETAIL = "last_402_detail"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_low_credit(
    *,
    balance_usd: float | None = None,
    threshold_usd: float | None = None,
    detail: str = "",
) -> None:
    """Set the low-credit warning (reactive 402 path).

    Called from stage error handlers when an OpenRouter 402
    insufficient-credit response is detected.  Preserves any existing
    *balance_usd* / *threshold_usd* from a prior proactive poll so the
    banner still shows the latest amount when a 402 fires between polls.
    """
    with _lock:
        _state[_KEY_LOW] = True
        _state[_KEY_LAST_402_AT] = _now_iso()
        _state[_KEY_LAST_402_DETAIL] = detail
        if balance_usd is not None:
            _state[_KEY_BALANCE] = balance_usd
        if threshold_usd is not None:
            _state[_KEY_THRESHOLD] = threshold_usd
        _state.setdefault(_KEY_BALANCE, 0.0)
        _state.setdefault(_KEY_THRESHOLD, 0.0)
        _state.setdefault(_KEY_LAST_CHECKED, None)


def record_balance_ok(*, balance_usd: float, threshold_usd: float) -> None:
    """Clear the low-credit warning (proactive poll sees healthy balance)."""
    with _lock:
        _state[_KEY_LOW] = False
        _state[_KEY_BALANCE] = balance_usd
        _state[_KEY_THRESHOLD] = threshold_usd
        _state[_KEY_LAST_CHECKED] = _now_iso()


def record_balance_low(
    *, balance_usd: float, threshold_usd: float, detail: str = ""
) -> None:
    """Set the low-credit warning from the proactive balance poll."""
    with _lock:
        _state[_KEY_LOW] = True
        _state[_KEY_BALANCE] = balance_usd
        _state[_KEY_THRESHOLD] = threshold_usd
        _state[_KEY_LAST_CHECKED] = _now_iso()
        if detail:
            _state[_KEY_LAST_402_DETAIL] = detail


def get_credit_status() -> dict[str, Any]:
    """Return a snapshot of the current credit-warning state.

    The returned dict is a shallow copy — safe for the caller to mutate.
    """
    with _lock:
        return {
            "low": _state.get(_KEY_LOW, False),
            "balance_usd": _state.get(_KEY_BALANCE),
            "threshold_usd": _state.get(_KEY_THRESHOLD),
            "last_checked": _state.get(_KEY_LAST_CHECKED),
            "last_402_at": _state.get(_KEY_LAST_402_AT),
            "detail": _state.get(_KEY_LAST_402_DETAIL, ""),
        }


def clear_credit_status() -> None:
    """Reset the warning to healthy (operator dismissed it)."""
    with _lock:
        _state[_KEY_LOW] = False
        _state[_KEY_LAST_402_AT] = None
        _state[_KEY_LAST_402_DETAIL] = ""
