"""Data-dir audit subpackage — periodic survey of ``.data/`` monotonic growth.

Re-exports the single public entry point (:func:`run_data_dir_audit_pass`)
and its result type (:class:`DataDirAuditPassResult`). Internal concerns
are split into focused modules:

- ``finders`` — top-N largest-items and unbounded-collection detection
- ``growth`` — growth-state persistence and delta computation
- ``orphans`` — orphan-workspace detection and workspace/clone GC
- ``filing`` — ticket-body builders and cross-pass dedup filing
- ``summary`` — rich summary generation for the runs panel

Seam: tests monkeypatch ``robotsix_mill.runners.data_dir_audit.Settings``
to inject fake settings instances. The :class:`Settings` import below
is kept precisely to expose that attribute on the module namespace for
monkeypatching.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ...config import RepoConfig, Settings
from ...core.service import TicketService

from .finders import (
    _collect_sizes,
    _select_largest_from_sizes,
    _self_healing_oversized_paths,
    check_unbounded_candidates,
)
from .filing import _file_findings_as_tickets
from .growth import _scan_growth_deltas
from .orphans import (
    _prune_archived_db_rows,
    _prune_closed_workspaces,
    _prune_orphan_workspaces,
    _prune_terminal_clones,
    _scan_orphan_workspaces,
)
from .summary import _build_summary

log = logging.getLogger("robotsix_mill.data_dir_audit")


@dataclass
class DataDirAuditPassResult:
    """Result of running a data-dir audit pass."""

    drafts_created: list[dict[str, Any]]  # [{"id": ..., "title": ...}]
    summary: str
    oversized_items: list[dict[str, Any]] = field(default_factory=list)
    # [{"path": "relative/path", "size_bytes": 123456, "is_directory": false}, …]
    updated_memory: str = ""
    session_id: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    growth_flags: list[dict[str, Any]] = field(default_factory=list)
    # Number of terminal-state ticket workspaces removed by the opt-in
    # prune_closed GC step (0 when the knob is disabled).
    closed_pruned: int = 0
    # Number of repo/ + repos/ clone dirs removed from terminal-ticket
    # workspaces by the default-on terminal-clone GC step.
    clones_pruned: int = 0
    # Number of archived ticket rows purged from mill.db files by the
    # default-on DB row GC step (enforces max_archived_tickets).
    db_rows_purged: int = 0
    # Number of orphan workspace directories removed by the default-on
    # orphan GC step (age-guarded via ticket-ID timestamp).
    orphans_pruned: int = 0


def run_data_dir_audit_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> DataDirAuditPassResult:
    """Execute one data-dir audit pass.

    Runs the top-N largest-items check (ticket 2), the
    unbounded-collection candidate check (ticket 4) over
    ``settings.data_dir``, the orphan-workspace check (ticket 5) over
    every board with a ``mill.db`` on disk, and the growth-delta check
    (ticket 3) which scans each board for size deltas against
    persisted prior-pass state. Inspection logic for the remaining
    checks (filing & dedup, rich summary) is added by child tickets
    6 and 7.

    Args:
        session_id: Langfuse session id from the poll loop (optional).
        repo_config: Per-repo config (optional — unused in this pass).

    Returns:
        ``DataDirAuditPassResult`` whose ``oversized_items`` list
        contains items above the configured size threshold, whose
        ``findings`` list contains flagged unbounded-collection
        candidates, whose ``growth_flags`` list contains the
        growth-delta flags from all scanned boards, and whose
        ``summary`` reflects the per-check counts.
    """
    # Settings is instantiated here so that any environment-variable
    # parsing errors surface early, and so tests can monkeypatch
    # ``robotsix_mill.runners.data_dir_audit.Settings`` to inject a
    # tmp_path-rooted instance.
    settings = Settings()

    # Default-on GC: prune reproducible repo/ + repos/ clones inside
    # terminal-ticket workspaces BEFORE size measurement, so reclaimed
    # space never flags growth. Preserves description.md / artifacts/.
    clones_pruned = 0
    if settings.data_dir_audit_prune_terminal_clones:
        clones_pruned = _prune_terminal_clones(settings)

    # Opt-in GC: prune whole workspace dirs of terminal-state
    # tickets BEFORE size measurement, so every downstream measurement
    # (oversized / growth / orphan) and therefore every filed alert
    # reflects the post-GC state. Default-off via the knob.
    closed_pruned = 0
    if settings.data_dir_audit_prune_closed:
        closed_pruned = _prune_closed_workspaces(settings)

    # Default-on DB row GC: purge oldest terminal-ticket rows (and
    # their events/comments/actions) from mill.db when the count
    # exceeds max_archived_tickets. This is a periodic safety net —
    # the reactive trigger on transition still fires, but stalled
    # boards (e.g. DONE never -> CLOSED) get cleaned here BEFORE
    # the growth scan so reclaimed space doesn't flag growth.
    db_rows_purged = 0
    if settings.data_dir_audit_prune_db_rows:
        db_rows_purged = _prune_archived_db_rows(settings)

    # Default-on GC: prune orphan workspace dirs (ticket absent from
    # the board DB) BEFORE size measurement, so reclaimed space never
    # flags growth and the subsequent orphan scan sees only young
    # orphans still within the age guard.
    orphans_pruned = 0
    if settings.data_dir_audit_prune_orphans:
        orphans_pruned = _prune_orphan_workspaces(settings)

    # Walk ``data_dir`` exactly once: the size dicts feed both the
    # top-N oversized check (ticket 2) and the summary header's
    # total-bytes / total-files anchor.
    if settings.data_dir.is_dir():
        file_sizes, dir_totals = _collect_sizes(settings.data_dir)
    else:
        file_sizes, dir_totals = {}, defaultdict(int)
    total_bytes = sum(file_sizes.values())
    total_files = len(file_sizes)

    # Compute the set of self-healing clone-cache / periodic-pass
    # paths so they are suppressed BEFORE top-N selection (mirroring
    # the growth check's existing exemption).
    suppressed = _self_healing_oversized_paths(file_sizes, dir_totals, settings)

    oversized = _select_largest_from_sizes(
        file_sizes,
        dir_totals,
        10,
        settings.data_dir_audit_size_threshold_bytes,
        suppressed=suppressed,
    )

    # Unbounded-collection candidate detection (ticket 4 of the epic).
    findings = check_unbounded_candidates(settings.data_dir, settings)

    # Orphan-workspace detection (ticket 5 of the epic). Ticket filing
    # is intentionally out of scope (ticket 6 consumes these findings
    # via the memory-ledger dedup path).
    orphans_by_board, total_orphans = _scan_orphan_workspaces(settings)

    # Growth-delta detection (ticket 3 of the epic).
    all_growth_flags, _boards_with_flags = _scan_growth_deltas(settings)

    # ----- Filing logic (ticket 6 of the epic) -----
    # Resolve a TicketService against the scheduling board. With no
    # repo_config or an empty board_id there is no board to file
    # against — skip filing entirely (still return the inspection
    # results so the runs panel can show what was found).
    drafts_created: list[dict[str, Any]] = []
    if repo_config is not None and repo_config.board_id:
        service = TicketService(settings, board_id=repo_config.board_id)
        drafts_created = _file_findings_as_tickets(
            settings,
            service,
            oversized,
            all_growth_flags,
            findings,
            session_id=session_id,
        )

    # ----- Summary covering all checks (ticket 7 of the epic) -----
    summary = _build_summary(
        total_bytes,
        total_files,
        oversized,
        all_growth_flags,
        findings,
        orphans_by_board,
        total_orphans,
        drafts_created,
        closed_pruned=closed_pruned,
        clones_pruned=clones_pruned,
        db_rows_purged=db_rows_purged,
        orphans_pruned=orphans_pruned,
    )

    log.info("data-dir audit pass done: %s", summary)

    return DataDirAuditPassResult(
        drafts_created=drafts_created,
        oversized_items=oversized,
        summary=summary,
        updated_memory="",
        session_id=session_id,
        findings=findings,
        growth_flags=all_growth_flags,
        closed_pruned=closed_pruned,
        clones_pruned=clones_pruned,
        db_rows_purged=db_rows_purged,
        orphans_pruned=orphans_pruned,
    )
