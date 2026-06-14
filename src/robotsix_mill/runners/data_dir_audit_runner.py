"""Backward-compatibility shim for the data-dir audit runner.

This module re-exports the public API from the ``data_dir_audit``
subpackage so existing string-reference consumers (e.g.
``poll_loops.py``) continue to resolve without changes.  New code
should import directly from ``robotsix_mill.runners.data_dir_audit``.
"""

from __future__ import annotations

from robotsix_mill.runners.data_dir_audit import (
    DataDirAuditPassResult,
    run_data_dir_audit_pass,
)
from robotsix_mill.runners.data_dir_audit.finders import (
    _CI_MONITOR_STATE_CAP_BYTES,
    _CI_PATTERNS_CAP_BYTES,
    _GENERIC_JSON_CAP_BYTES,
    _RUNS_JSON_CAP_BYTES,
    _RUNS_JSON_MAX_ENTRIES,
    check_unbounded_candidates,
    find_largest_items,
)
from robotsix_mill.runners.data_dir_audit.filing import (
    _build_finding,
    _build_growth_finding,
    _build_orphan_finding,
    _build_oversized_finding,
    _build_unbounded_finding,
    _file_findings_as_tickets,
    _human_bytes,
)
from robotsix_mill.runners.data_dir_audit.growth import (
    _compute_growth_deltas,
    _enumerate_boards,
    _growth_state_path,
    _is_meta_clone_cache_path,
    _is_periodic_pass_workspace_path,
    _load_growth_state,
    _save_growth_state,
    _scan_board_sizes,
    _workspace_ticket_id_for_path,
)
from robotsix_mill.runners.data_dir_audit.orphans import (
    OrphanWorkspace,
    _prune_closed_workspaces,
    find_orphan_workspaces,
)

__all__ = [
    "DataDirAuditPassResult",
    "OrphanWorkspace",
    "_CI_MONITOR_STATE_CAP_BYTES",
    "_CI_PATTERNS_CAP_BYTES",
    "_GENERIC_JSON_CAP_BYTES",
    "_RUNS_JSON_CAP_BYTES",
    "_RUNS_JSON_MAX_ENTRIES",
    "_build_finding",
    "_build_growth_finding",
    "_build_orphan_finding",
    "_build_oversized_finding",
    "_build_unbounded_finding",
    "_compute_growth_deltas",
    "_enumerate_boards",
    "_file_findings_as_tickets",
    "_growth_state_path",
    "_human_bytes",
    "_is_meta_clone_cache_path",
    "_is_periodic_pass_workspace_path",
    "_load_growth_state",
    "_prune_closed_workspaces",
    "_save_growth_state",
    "_scan_board_sizes",
    "_workspace_ticket_id_for_path",
    "check_unbounded_candidates",
    "find_largest_items",
    "find_orphan_workspaces",
    "run_data_dir_audit_pass",
]
