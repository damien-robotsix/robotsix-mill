"""Data-dir GC — deterministic periodic disk reclamation.

Runs exactly 5 GC steps in order, then returns a short summary.
No size scanning, growth deltas, or ticket filing — those concerns
live in robotsix-central-deploy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...config import RepoConfig, Settings

from .orphans import (
    _prune_archived_db_rows,
    _prune_closed_workspaces,
    _prune_orphan_workspaces,
    _prune_oversized_memory_ledgers,
    _prune_terminal_clones,
)

log = logging.getLogger("robotsix_mill.data_dir_gc")


@dataclass
class DataDirGcPassResult:
    """Result of running a data-dir GC pass."""

    closed_pruned: int = 0
    clones_pruned: int = 0
    db_rows_purged: int = 0
    orphans_pruned: int = 0
    memory_ledgers_truncated: int = 0
    summary: str = ""


def run_data_dir_gc_pass(  # noqa: C901
    session_id: str = "",
    repo_config: RepoConfig | None = None,
    settings: Settings | None = None,
) -> DataDirGcPassResult:
    """Execute one data-dir GC pass — 5 reclaim steps, no audit.

    Args:
        session_id: Langfuse session id from the poll loop (unused —
            the GC pass iterates every board on disk independently).
        repo_config: Per-repo configuration from the poll loop (unused
            for the same reason — the pass is board-agnostic).
        settings: Settings instance (optional; instantiated here if None,
            so tests can monkeypatch).

    Returns:
        ``DataDirGcPassResult`` with per-step counters and a summary line.
    """
    if settings is None:
        settings = Settings()

    # 1. Default-on: prune terminal-ticket clone dirs (repo/, repos/)
    clones_pruned = 0
    if settings.data_dir_gc_prune_terminal_clones:
        clones_pruned = _prune_terminal_clones(settings)

    # 2. Opt-in: prune whole workspace dirs of terminal-state tickets
    closed_pruned = 0
    if settings.data_dir_gc_prune_closed:
        closed_pruned = _prune_closed_workspaces(settings)

    # 3. Default-on: purge oldest terminal-ticket DB rows
    db_rows_purged = 0
    if settings.data_dir_gc_prune_db_rows:
        db_rows_purged = _prune_archived_db_rows(settings)

    # 4. Default-on: prune orphan workspace dirs
    orphans_pruned = 0
    if settings.data_dir_gc_prune_orphans:
        orphans_pruned = _prune_orphan_workspaces(settings)

    # 5. Default-on: truncate over-cap *_memory.md files
    memory_ledgers_truncated = 0
    if settings.data_dir_gc_prune_memory_ledgers:
        memory_ledgers_truncated = _prune_oversized_memory_ledgers(settings)

    # Build summary line
    parts: list[str] = []
    if clones_pruned:
        parts.append(f"clones={clones_pruned}")
    if closed_pruned:
        parts.append(f"closed={closed_pruned}")
    if db_rows_purged:
        parts.append(f"db_rows={db_rows_purged}")
    if orphans_pruned:
        parts.append(f"orphans={orphans_pruned}")
    if memory_ledgers_truncated:
        parts.append(f"memory_ledgers={memory_ledgers_truncated}")
    summary = "data-dir GC: " + (", ".join(parts) if parts else "nothing to reclaim")

    log.info(summary)

    return DataDirGcPassResult(
        closed_pruned=closed_pruned,
        clones_pruned=clones_pruned,
        db_rows_purged=db_rows_purged,
        orphans_pruned=orphans_pruned,
        memory_ledgers_truncated=memory_ledgers_truncated,
        summary=summary,
    )
