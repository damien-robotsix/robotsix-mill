"""Event-driven worker. No scheduler.

A ticket is enqueued the moment it is emitted (or transitions into an
actionable state). A consumer pulls it and **chains** stages —
``draft → … → done`` — until it hits a terminal state, a stub, or an
error. A bounded **pool** of consumers (``MILL_MAX_CONCURRENCY``) runs
distinct tickets in parallel; a dedupe set guarantees one ticket is
never processed by two consumers at once (one ticket's stages still
run sequentially within its own consumer).

This package was split from a single ``worker.py`` module into focused
submodules organized by responsibility. This ``__init__`` re-exports the
full public + internal symbol surface so existing import sites keep
resolving unchanged.
"""

from __future__ import annotations

import asyncio  # re-exported for ``robotsix_mill.runtime.worker.asyncio`` access

from .core import Worker
from .epic import (
    _apply_dep_updates,
    _branch_is_stale,
    _build_child_summaries,
    _fetch_draft_child,
    _handle_epic_decision,
    _reconcile_child_changes,
    _resolve_delivery,
    _run_epic_reeval,
    _run_epic_reprocess,
    _validate_epic_state,
)
from .processing import (
    log,
    process_ticket,
    _maybe_reevaluate_epic,
    _process_ticket_inner,
    _spawn_epic_reeval,
)

__all__ = [
    "asyncio",
    "log",
    "Worker",
    "process_ticket",
    "_process_ticket_inner",
    "_maybe_reevaluate_epic",
    "_spawn_epic_reeval",
    "_run_epic_reeval",
    "_run_epic_reprocess",
    "_validate_epic_state",
    "_build_child_summaries",
    "_apply_dep_updates",
    "_handle_epic_decision",
    "_fetch_draft_child",
    "_reconcile_child_changes",
    "_resolve_delivery",
    "_branch_is_stale",
]
