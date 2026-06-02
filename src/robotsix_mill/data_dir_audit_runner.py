"""Data-dir audit runner — periodic survey of ``.data/`` monotonic growth.

This is the scaffold (ticket 1 of the epic) for a daily periodic agent
that surveys ``.data/`` for monotonic growth and files draft tickets
when it finds problems. The actual inspection logic (top-N largest
items, growth deltas, unbounded-collection candidates, orphan
workspaces, ticket filing & dedup, rich summary) is added by child
tickets 2–7.

Until those land, ``run_data_dir_audit_pass`` is a no-op that simply
returns a ``DataDirAuditPassResult`` with empty findings.

Seam: tests monkeypatch ``robotsix_mill.data_dir_audit_runner.Settings``
to inject fake settings instances. The :class:`Settings` import below
is kept (``# noqa: F401`` is not needed since :class:`Settings` is
also instantiated at runtime) precisely to expose that attribute on
the module namespace for monkeypatching.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import select

from .config import RepoConfig, Settings
from .core import db
from .core.models import Ticket

log = logging.getLogger("robotsix_mill.data_dir_audit")

# Lenient ticket-ID prefix check: only the leading timestamp is
# validated (``YYYYmmddTHHMMSSZ-``). The suffix is left unparsed so
# future ID-format changes do not produce false negatives, while
# obviously non-ticket directories (``.gitkeep``, ``artifacts``, …)
# are still filtered out.
_TICKET_ID_PREFIX_RE = re.compile(r"^\d{8}T\d{6}Z-")

# Maximum number of ticket IDs per ``SELECT … WHERE id IN (…)`` batch.
# Keeps the IN-clause small enough for SQLite while still avoiding the
# one-query-per-directory anti-pattern.
_BATCH_SIZE = 500


@dataclass
class OrphanWorkspace:
    """A workspace directory whose ticket no longer exists in the DB."""

    board_id: str
    ticket_id: str
    path: Path
    dir_size_bytes: int


@dataclass
class DataDirAuditPassResult:
    """Result of running a data-dir audit pass."""

    drafts_created: list[dict]  # [{"id": ..., "title": ...}]
    summary: str
    updated_memory: str = ""
    session_id: str = ""


def _dir_size_bytes(path: Path) -> int:
    """Approximate on-disk size of *path*.

    Sums ``stat().st_size`` for every regular file under *path* via
    ``rglob``. No deduplication for hardlinks and no filesystem-level
    block accounting — acceptable for a detection heuristic
    (per the ticket spec).
    """
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                # Skip files that vanish between rglob and stat.
                continue
    except OSError:
        return total
    return total


def _boards_from_disk(settings: Settings) -> list[str]:
    """Return board IDs that have a ``mill.db`` on disk.

    Mirrors the pattern from ``verify_runner`` and
    ``timeout_escalation_runner``: only boards that have actually been
    materialised on disk are scanned. Registered-but-not-yet-created
    boards are skipped because they have no DB to cross-reference.
    """
    boards: list[str] = []
    try:
        for child in sorted(settings.data_dir.iterdir()):
            if child.is_dir() and (child / "mill.db").exists():
                boards.append(child.name)
    except OSError:
        pass
    return boards


def find_orphan_workspaces(
    settings: Settings,
    board_id: str,
) -> list[OrphanWorkspace]:
    """Return workspace directories whose ticket no longer exists.

    Lists every subdirectory under ``<data_dir>/<board_id>/workspaces/``,
    cross-references the names against *board_id*'s ``mill.db`` in one
    batched ``SELECT … WHERE id IN (…)`` per batch (batch size
    ``≤ 500``), and returns an :class:`OrphanWorkspace` for each
    directory whose ticket ID is absent from the DB.

    The function is board-scoped: a workspace directory in board ``A``
    is never compared against board ``B``'s DB.

    Returns an empty list when the workspaces directory does not
    exist (e.g. fresh board with zero tickets), when it is empty, or
    when every subdirectory corresponds to a live ticket.

    Subdirectories whose name does not match the ticket-ID timestamp
    prefix (``^\\d{8}T\\d{6}Z-``) are skipped with a ``WARNING`` log
    rather than counted as orphans — this filters obviously-non-ticket
    entries like ``.gitkeep`` or ``artifacts`` without crashing.
    """
    workspaces_dir = settings.workspaces_dir_for(board_id)
    if not workspaces_dir.exists():
        return []

    candidates: list[tuple[str, Path]] = []
    try:
        for child in sorted(workspaces_dir.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if not _TICKET_ID_PREFIX_RE.match(name):
                log.warning(
                    "data_dir_audit: board=%r — skipping non-ticket-ID "
                    "directory %r in workspaces/",
                    board_id,
                    name,
                )
                continue
            candidates.append((name, child))
    except OSError:
        return []

    if not candidates:
        return []

    # Batched DB cross-reference: collect the set of IDs that exist
    # in the DB, then diff against the on-disk candidates.
    candidate_ids = [name for name, _ in candidates]
    existing_ids: set[str] = set()
    with db.session(settings, board_id) as s:
        for start in range(0, len(candidate_ids), _BATCH_SIZE):
            chunk = candidate_ids[start : start + _BATCH_SIZE]
            stmt = select(Ticket.id).where(Ticket.id.in_(chunk))
            existing_ids.update(s.exec(stmt).all())

    orphans: list[OrphanWorkspace] = []
    for name, path in candidates:
        if name in existing_ids:
            continue
        orphans.append(
            OrphanWorkspace(
                board_id=board_id,
                ticket_id=name,
                path=path,
                dir_size_bytes=_dir_size_bytes(path),
            )
        )
    return orphans


def run_data_dir_audit_pass(
    session_id: str = "",
    repo_config: RepoConfig | None = None,
) -> DataDirAuditPassResult:
    """Execute one data-dir audit pass.

    This scaffold returns an empty result. Inspection logic is added
    by child tickets 2–7 of the epic.

    Args:
        session_id: Langfuse session id from the poll loop (optional).
        repo_config: Per-repo config (optional).

    Returns:
        ``DataDirAuditPassResult`` with empty findings.
    """
    # Settings is intentionally instantiated even though the scaffold
    # has no inspection logic yet — this preserves the monkeypatch
    # seam (tests stub ``robotsix_mill.data_dir_audit_runner.Settings``)
    # and surfaces any environment-variable parsing errors early.
    settings = Settings()

    # Orphan-workspace detection (ticket 5 of the epic).
    # Iterate over every board with a ``mill.db`` on disk; ticket
    # filing is intentionally out of scope (ticket 6 consumes these
    # findings via the memory-ledger dedup path).
    orphans_by_board: dict[str, list[OrphanWorkspace]] = {}
    total_orphans = 0
    for board_id in _boards_from_disk(settings):
        try:
            found = find_orphan_workspaces(settings, board_id)
        except Exception:
            log.warning(
                "data_dir_audit: board=%r — orphan workspace scan failed",
                board_id,
                exc_info=True,
            )
            continue
        if found:
            orphans_by_board[board_id] = found
            total_orphans += len(found)
            for o in found:
                log.info(
                    "data_dir_audit: orphan workspace board=%r ticket=%s "
                    "path=%s size=%dB",
                    board_id,
                    o.ticket_id,
                    o.path,
                    o.dir_size_bytes,
                )

    if total_orphans == 0:
        summary = "no findings"
    else:
        per_board = ", ".join(
            f"{board}={len(items)}" for board, items in sorted(orphans_by_board.items())
        )
        summary = f"orphan workspaces: {total_orphans} ({per_board})"

    return DataDirAuditPassResult(
        drafts_created=[],
        summary=summary,
        updated_memory="",
        session_id=session_id,
    )
