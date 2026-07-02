"""Process-wide cap on concurrent Claude Agent SDK runs.

The Claude SDK transport spawns a ``claude`` CLI subprocess per run. When many
worker consumers start runs at once (e.g. a busy worker fanning out across
several repos at startup), the simultaneous subprocess spawns contend for
resources and one run can stall — hanging without making progress. A single
global semaphore bounds how many Claude runs execute concurrently across the
whole process, smoothing that spawn storm so the contention never arises.

Only the synchronous, top-level ``run_sync()`` path is bounded:

- ``run_sync`` is where a worker stage actually spawns its CLI subprocess — the
  contention point — and it holds the permit for the whole run, so at most
  *limit* runs are in flight at once.
- ``run_sync`` calls ``asyncio.run`` internally, so it can only execute at the
  top of a worker thread, never nested inside another agent's async tool.
  Bounding it therefore cannot deadlock against a nested subagent: those use the
  async ``run()`` path, which is intentionally left unbounded (a parent holding
  a permit while waiting on a child that needs one would otherwise deadlock once
  the cap is reached).

The single **heavy-work** semaphore (``get_claude_run_semaphore``) bounds
implement/audit/refine Claude runs, sized by ``claude_max_concurrency``.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_semaphore: threading.BoundedSemaphore | None = None
_configured_limit: int | None = None


def get_claude_run_semaphore(limit: int) -> threading.BoundedSemaphore:
    """Return the process-wide semaphore bounding concurrent Claude runs.

    Sized once, on first use, to ``max(1, limit)``. Later calls reuse the
    existing semaphore and ignore a differing *limit* (the cap can't silently
    grow or shrink at runtime); :func:`reset_for_tests` clears it between tests.
    """
    global _semaphore, _configured_limit
    with _lock:
        if _semaphore is None:
            _configured_limit = max(1, limit)
            _semaphore = threading.BoundedSemaphore(_configured_limit)
        return _semaphore


def reset_for_tests() -> None:
    """Drop the cached semaphore so the next call re-sizes it. Tests only."""
    global _semaphore, _configured_limit
    with _lock:
        _semaphore = None
        _configured_limit = None


class _BoundedClaudeHandle:
    """Wrap a Claude SDK agent handle so ``run_sync`` acquires the global
    concurrency semaphore for the duration of the run. Every other access — the
    async ``run``, ``close``, ``.output``-bearing results, etc. — delegates
    unchanged to the wrapped handle."""

    def __init__(self, handle: Any, semaphore: threading.BoundedSemaphore) -> None:
        # Set via the instance dict directly so __getattr__ (which reads
        # self._handle) can never recurse during construction.
        self._handle = handle
        self._semaphore = semaphore

    def run_sync(self, *args: Any, **kwargs: Any) -> Any:
        with self._semaphore:
            return self._handle.run_sync(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined on this wrapper (run_sync,
        # _handle, _semaphore are), so this delegates run/close/etc.
        return getattr(self._handle, name)


def bound_claude_handle(handle: Any, limit: int) -> _BoundedClaudeHandle:
    """Wrap *handle* so its top-level ``run_sync`` calls share the process-wide
    Claude-run semaphore sized to *limit*."""
    return _BoundedClaudeHandle(handle, get_claude_run_semaphore(limit))
