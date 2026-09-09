"""Cross-thread stage-progress registry for the progress-aware deadline.

The per-stage soft deadline in ``processing.py`` must distinguish an
*idle stall* (the agent hung, no model/tool activity) from a
*slow-but-progressing* run that is still actively making tool calls.
``coordinating.py`` already sets a ``threading.Event`` before every tool
call for the ``implement_pass_timeout`` watchdog, but that signal lives
in the worker thread and never reaches the stage-level deadline running
in the event loop.

This module is that bridge: a ``threading.Lock``-guarded registry
mapping ``(ticket_id, stage_name)`` → last-progress ``time.monotonic()``
timestamp. The writer is the ``stage.run`` worker thread (via the tool
wrappers in ``coordinating.py``); the reader is the event loop's polling
deadline wait. Pure ``threading``/``time`` — no asyncio dependency.
"""

from __future__ import annotations

import contextlib
import threading
import time

_lock = threading.Lock()
_last_activity: dict[tuple[str, str], float] = {}


def begin(ticket_id: str, stage_name: str) -> None:
    """Register the (ticket, stage) run and stamp progress at ``now``."""
    with _lock:
        _last_activity[ticket_id, stage_name] = time.monotonic()


def mark(ticket_id: str, stage_name: str) -> None:
    """Re-stamp progress for the (ticket, stage) run.

    Called from inside the tool wrappers on the worker thread, so it MUST
    never raise — a failure here would propagate into the agent's tool
    call. Unknown keys are stamped anyway (harmless).
    """
    # Progress tracking is best-effort; never break a tool call.
    with contextlib.suppress(Exception), _lock:
        _last_activity[ticket_id, stage_name] = time.monotonic()


def last_activity_age(ticket_id: str, stage_name: str) -> float | None:
    """Seconds since the last ``begin``/``mark`` for the run.

    ``None`` when the run was never begun/marked (no instrumentation, or
    already cleared).
    """
    with _lock:
        stamp = _last_activity.get((ticket_id, stage_name))
    if stamp is None:
        return None
    return time.monotonic() - stamp


def clear(ticket_id: str, stage_name: str) -> None:
    """Drop the (ticket, stage) entry from the registry."""
    with _lock:
        _last_activity.pop((ticket_id, stage_name), None)
