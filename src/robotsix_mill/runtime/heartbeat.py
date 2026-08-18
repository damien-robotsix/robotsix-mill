"""Crash-diagnostic heartbeat.

The mill process dies abruptly under memory pressure (kernel OOM /
SIGKILL) with no Python traceback and no uvicorn shutdown lines, so the
container logs alone cannot distinguish an OOM kill from a clean
operator restart.  This module persists a small heartbeat marker in the
data directory so the *next* process can detect how the previous one
ended:

* ``write_heartbeat(data_dir)`` records ``state="running"`` with the
  current PID and a timestamp, and is re-called periodically while the
  process is alive.
* ``mark_clean_shutdown(data_dir)`` flips the marker to
  ``state="stopped"`` during graceful teardown (the lifespan ``finally``
  block).
* ``check_previous_death(data_dir)`` is called at startup *before* the
  first fresh ``write_heartbeat``.  A marker left in ``state="running"``
  proves the previous process never reached graceful teardown, so the
  startup log can say "previous process died abruptly" instead of the
  crash being indistinguishable from a restart.

The OOM hint is best-effort: when running under cgroup v2 the kernel's
``memory.events`` ``oom_kill`` counter is read and, if non-zero, the
abrupt-death message is annotated as a suspected OOM kill.  Absence of
the file (cgroup v1, non-container host) degrades to a plain
abrupt-death message with no false claim.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("robotsix_mill.runtime.heartbeat")

HEARTBEAT_FILENAME = "heartbeat.json"
_STATE_RUNNING = "running"
_STATE_STOPPED = "stopped"


def _marker_path(data_dir: Path) -> Path:
    return data_dir / HEARTBEAT_FILENAME


def write_heartbeat(data_dir: Path) -> None:
    """Write (or refresh) the heartbeat marker in ``state="running"``.

    Best-effort: an ``OSError`` (read-only data dir, ENOSPC) is logged
    and swallowed — a heartbeat failure must never take the worker down.
    """
    payload = {
        "state": _STATE_RUNNING,
        "pid": os.getpid(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    marker = _marker_path(data_dir)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        log.warning("heartbeat: could not write %s", marker, exc_info=True)


def mark_clean_shutdown(data_dir: Path) -> None:
    """Flip the heartbeat marker to ``state="stopped"``.

    Called from graceful teardown so the next startup does not report an
    abrupt death.  Best-effort — an ``OSError`` is logged and swallowed.
    """
    payload = {
        "state": _STATE_STOPPED,
        "pid": os.getpid(),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    marker = _marker_path(data_dir)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        log.warning(
            "heartbeat: could not write shutdown marker %s", marker, exc_info=True
        )


def read_heartbeat(data_dir: Path) -> dict[str, object] | None:
    """Read the previous process's heartbeat marker, or ``None``.

    Returns ``None`` when the marker is absent (first boot) or corrupt
    (unreadable / malformed JSON) — both are treated as "no previous
    evidence", never as a crash.
    """
    marker = _marker_path(data_dir)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _oom_kill_count() -> int | None:
    """Return the cgroup v2 ``oom_kill`` counter, or ``None`` when
    unavailable (cgroup v1, non-container host, or unreadable).

    The counter is monotonically increasing per container cgroup and
    survives Docker's same-container restart, so a non-zero value at
    startup is a strong (but not exclusive) OOM signal.
    """
    try:
        text = Path("/sys/fs/cgroup/memory.events").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("oom_kill "):
            try:
                return int(line.split()[1])
            except (ValueError, IndexError):
                return None
    return None


def check_previous_death(data_dir: Path) -> str | None:
    """Detect an abrupt previous-process death at startup.

    Returns a one-line diagnostic string when the previous process left
    a ``state="running"`` marker (i.e. it never reached graceful
    teardown), or ``None`` when the marker is absent or records a clean
    shutdown.  Callers log the returned string at WARNING level.
    """
    prev = read_heartbeat(data_dir)
    if prev is None:
        return None
    if prev.get("state") == _STATE_STOPPED:
        return None

    pid = prev.get("pid", "?")
    last_beat = prev.get("updated_at", "unknown")

    oom_hint = ""
    oom_count = _oom_kill_count()
    if oom_count is not None and oom_count > 0:
        oom_hint = f"; cgroup oom_kill count is {oom_count} — suspected OOM kill"

    return (
        f"previous process (PID {pid}) died abruptly at or after "
        f"{last_beat} — no clean shutdown marker was written{oom_hint}"
    )
