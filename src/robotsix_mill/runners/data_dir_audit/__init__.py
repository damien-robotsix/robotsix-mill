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
from pathlib import Path
from typing import Any

from ...config import RepoConfig, Settings
from ...config.repo_settings import load_repo_data_dir
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


def _find_any_clone_dir(data_dir: Path, repo_id: str) -> Path | None:
    """Return any existing clone of *repo_id* under *data_dir*, or ``None``.

    Looks for ``<data_dir>/<repo_id>/<anything>_workspace/repo/.git``
    and returns the ``.../repo`` directory when found.  This mirrors
    the periodic supervisor's ``_find_config_clone_dir`` logic so the
    data-dir audit runner can read per-repo settings from
    ``.robotsix-mill/config.yaml`` without owning its own clone.
    """
    base = data_dir / repo_id
    if not base.is_dir():
        return None
    # Preferred: the periodic supervisor's own clone.
    periodic = base / "periodic_workspace" / "repo"
    if (periodic / ".git").exists():
        return periodic
    # Legacy name (pre-rename).
    bespoke = base / "bespoke_workspace" / "repo"
    if (bespoke / ".git").exists():
        return bespoke
    # Any other worker clone.
    try:
        for child in base.iterdir():
            if (
                child.is_dir()
                and child.name.endswith("_workspace")
                and (child / "repo" / ".git").exists()
            ):
                return child / "repo"
    except OSError:
        pass
    return None


def _resolve_audit_data_dir(
    settings: Settings,
    repo_config: RepoConfig | None,
) -> tuple[Path, Settings]:
    """Resolve the effective data directory and settings for this pass.

    When *repo_config* is provided and its repo has an existing clone
    on disk whose ``.robotsix-mill/config.yaml`` declares a ``data_dir``
    key, that per-repo value is used as the audit target directory.
    Otherwise falls back to the global ``settings.data_dir``.

    Returns ``(audit_data_dir, audit_settings)`` where *audit_settings*
    is a copy of *settings* with ``data_dir`` rebound when an override
    is active, or the original *settings* otherwise.
    """
    audit_data_dir: Path = settings.data_dir
    if repo_config is not None:
        clone_dir = _find_any_clone_dir(settings.data_dir, repo_config.repo_id)
        if clone_dir is not None:
            per_repo = load_repo_data_dir(clone_dir)
            if per_repo is not None:
                audit_data_dir = per_repo
    if audit_data_dir != settings.data_dir:
        audit_settings = settings.model_copy(update={"data_dir": audit_data_dir})
    else:
        audit_settings = settings
    return audit_data_dir, audit_settings


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

    # Resolve per-repo data_dir from .robotsix-mill/config.yaml when
    # available.  Falls back to the global settings.data_dir when the
    # repo has no clone on disk or declares no data_dir override.
    audit_data_dir, audit_settings = _resolve_audit_data_dir(settings, repo_config)

    # Default-on GC: prune reproducible repo/ + repos/ clones inside
    # terminal-ticket workspaces BEFORE size measurement, so reclaimed
    # space never flags growth. Preserves description.md / artifacts/.
    clones_pruned = 0
    if audit_settings.data_dir_audit_prune_terminal_clones:
        clones_pruned = _prune_terminal_clones(audit_settings)

    # Opt-in GC: prune whole workspace dirs of terminal-state
    # tickets BEFORE size measurement, so every downstream measurement
    # (oversized / growth / orphan) and therefore every filed alert
    # reflects the post-GC state. Default-off via the knob.
    closed_pruned = 0
    if audit_settings.data_dir_audit_prune_closed:
        closed_pruned = _prune_closed_workspaces(audit_settings)

    # Default-on DB row GC: purge oldest terminal-ticket rows (and
    # their events/comments/actions) from mill.db when the count
    # exceeds max_archived_tickets. This is a periodic safety net —
    # the reactive trigger on transition still fires, but stalled
    # boards (e.g. DONE never -> CLOSED) get cleaned here BEFORE
    # the growth scan so reclaimed space doesn't flag growth.
    db_rows_purged = 0
    if audit_settings.data_dir_audit_prune_db_rows:
        db_rows_purged = _prune_archived_db_rows(audit_settings)

    # Default-on GC: prune orphan workspace dirs (ticket absent from
    # the board DB) BEFORE size measurement, so reclaimed space never
    # flags growth and the subsequent orphan scan sees only young
    # orphans still within the age guard.
    orphans_pruned = 0
    if audit_settings.data_dir_audit_prune_orphans:
        orphans_pruned = _prune_orphan_workspaces(audit_settings)

    # Walk ``data_dir`` exactly once: the size dicts feed both the
    # top-N oversized check (ticket 2) and the summary header's
    # total-bytes / total-files anchor.
    if audit_data_dir.is_dir():
        file_sizes, dir_totals = _collect_sizes(audit_data_dir)
    else:
        file_sizes, dir_totals = {}, defaultdict(int)
    total_bytes = sum(file_sizes.values())
    total_files = len(file_sizes)

    # Compute the set of self-healing clone-cache / periodic-pass
    # paths so they are suppressed BEFORE top-N selection (mirroring
    # the growth check's existing exemption).
    suppressed = _self_healing_oversized_paths(file_sizes, dir_totals, audit_settings)

    oversized = _select_largest_from_sizes(
        file_sizes,
        dir_totals,
        10,
        audit_settings.data_dir_audit_size_threshold_bytes,
        suppressed=suppressed,
    )

    # Unbounded-collection candidate detection (ticket 4 of the epic).
    findings = check_unbounded_candidates(audit_data_dir, audit_settings)

    # Orphan-workspace detection (ticket 5 of the epic). Ticket filing
    # is intentionally out of scope (ticket 6 consumes these findings
    # via the memory-ledger dedup path).
    orphans_by_board, total_orphans = _scan_orphan_workspaces(audit_settings)

    # Growth-delta detection (ticket 3 of the epic).
    all_growth_flags, _boards_with_flags = _scan_growth_deltas(audit_settings)

    # ----- Filing logic (ticket 6 of the epic) -----
    # Resolve a TicketService against the scheduling board. With no
    # repo_config or an empty board_id there is no board to file
    # against — skip filing entirely (still return the inspection
    # results so the runs panel can show what was found).
    # NOTE: TicketService always uses the original *settings* (not
    # *audit_settings*) because the board DB lives under the global
    # data_dir, not the per-repo override.
    drafts_created: list[dict[str, Any]] = []
    if repo_config is not None and repo_config.board_id:
        service = TicketService(settings, board_id=repo_config.board_id)
        drafts_created = _file_findings_as_tickets(
            audit_settings,
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
