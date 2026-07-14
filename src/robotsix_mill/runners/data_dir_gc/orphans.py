"""Orphan-workspace detection and workspace/clone GC (ticket 5).

Detects workspace directories whose ticket no longer exists in the DB,
and provides opt-in closed-workspace pruning plus default-on
terminal-clone GC.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlmodel import select

from ...config import Settings
from ...core import db
from ...core.db import retry_on_db_full
from ...core.models import Comment, Ticket, TicketEvent, _now
from ...core.states import State

log = logging.getLogger("robotsix_mill.data_dir_gc")

# Lenient ticket-ID prefix check: only the leading timestamp is
# validated (``YYYYmmddTHHMMSSZ-``).
_TICKET_ID_PREFIX_RE = re.compile(r"^\d{8}T\d{6}Z-")

# Maximum number of ticket IDs per SELECT ... WHERE id IN (...) batch.
_BATCH_SIZE = 500

# Terminal ticket states: those with empty outgoing transition sets.
_TERMINAL_STATES = {State.CLOSED, State.EPIC_CLOSED, State.ANSWERED}


@dataclass
class OrphanWorkspace:
    """A workspace directory whose ticket no longer exists in the DB."""

    board_id: str
    ticket_id: str
    path: Path
    dir_size_bytes: int


# ---------------------------------------------------------------------------
# Orphan-workspace detection (ticket 5)
# ---------------------------------------------------------------------------


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
                    "data_dir_gc: board=%r — skipping non-ticket-ID "
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
            stmt = select(Ticket.id).where(Ticket.id.in_(chunk))  # type: ignore[attr-defined]
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


# ---------------------------------------------------------------------------
# Opt-in GC: prune workspaces of terminal-state tickets
# ---------------------------------------------------------------------------


def _close_time_from_ticket_id(name: str) -> datetime | None:
    """Parse a tz-aware close time from a ticket-ID timestamp prefix.

    The prefix is ``YYYYmmddTHHMMSSZ-`` (16 chars before the dash).
    Returns ``None`` when the prefix does not parse — a defensive
    fallback used only when no terminal ``TicketEvent`` exists.
    """
    try:
        return datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _workspace_candidates(workspaces_dir: Path) -> list[tuple[str, Path]]:
    """List ``(ticket_id, path)`` for ticket-ID-named workspace subdirs."""
    candidates: list[tuple[str, Path]] = []
    for child in sorted(workspaces_dir.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if not _TICKET_ID_PREFIX_RE.match(name):
            continue
        candidates.append((name, child))
    return candidates


def _terminal_close_times(
    settings: Settings,
    board_id: str,
    candidate_ids: list[str],
) -> tuple[set[str], dict[str, datetime]]:
    """Cross-reference *candidate_ids* against *board_id*'s DB in batched
    ``IN`` selects.

    Returns ``(terminal_ids, close_times)`` where ``terminal_ids`` are
    the candidate IDs whose ticket exists AND is in a terminal state,
    and ``close_times`` maps each such ID to the max ``at`` of its
    terminal ``TicketEvent`` rows (the close time).
    """
    terminal_ids: set[str] = set()
    close_times: dict[str, datetime] = {}
    with db.session(settings, board_id) as s:
        for start in range(0, len(candidate_ids), _BATCH_SIZE):
            chunk = candidate_ids[start : start + _BATCH_SIZE]
            chunk_terminal = set(
                s.exec(
                    select(Ticket.id).where(
                        Ticket.id.in_(chunk),  # type: ignore[attr-defined]
                        Ticket.state.in_(_TERMINAL_STATES),  # type: ignore[attr-defined]
                    )
                ).all()
            )
            if not chunk_terminal:
                continue
            terminal_ids.update(chunk_terminal)
            rows = s.exec(
                select(TicketEvent.ticket_id, TicketEvent.at).where(
                    TicketEvent.ticket_id.in_(chunk_terminal),  # type: ignore[attr-defined]
                    TicketEvent.state.in_(_TERMINAL_STATES),  # type: ignore[attr-defined]
                )
            ).all()
            for ticket_id, at in rows:
                # Keep the most recent terminal-event time per ticket.
                prior = close_times.get(ticket_id)
                if prior is None or at > prior:
                    close_times[ticket_id] = at
    return terminal_ids, close_times


def _prune_board_workspaces(
    settings: Settings,
    board_id: str,
    now: datetime,
    age_threshold_seconds: int,
) -> int:
    """Remove terminal-state ticket workspaces for one board.

    Mirrors :func:`find_orphan_workspaces`: lists workspace subdirs,
    skips non-ticket-ID names, and cross-references the board DB in
    batched ``IN`` selects. A directory is removed only when its ticket
    is present AND in a terminal state AND its close time is at least
    *age_threshold_seconds* old. Returns the number of dirs removed.
    """
    workspaces_dir = settings.workspaces_dir_for(board_id)
    if not workspaces_dir.exists():
        return 0

    candidates = _workspace_candidates(workspaces_dir)
    if not candidates:
        return 0

    candidate_ids = [name for name, _ in candidates]
    terminal_ids, close_times = _terminal_close_times(settings, board_id, candidate_ids)

    removed = 0
    for name, path in candidates:
        if name not in terminal_ids:
            continue
        close_time = close_times.get(name) or _close_time_from_ticket_id(name)
        if close_time is None:
            continue
        if (now - close_time).total_seconds() < age_threshold_seconds:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed += 1
            log.info(
                "data_dir_gc: pruned closed workspace board=%r ticket=%s path=%s",
                board_id,
                name,
                path,
            )
    if removed:
        log.info(
            "data_dir_gc: board=%r pruned %d closed workspace(s)",
            board_id,
            removed,
        )
    return removed


def _prune_closed_workspaces(settings: Settings) -> int:
    """Remove workspace dirs of terminal-state tickets older than the
    configured age. Returns the number of directories removed."""
    now = _now()
    age_threshold_seconds = settings.data_dir_gc_prune_closed_age_seconds
    total_removed = 0
    for board_id in _boards_from_disk(settings):
        try:
            total_removed += _prune_board_workspaces(
                settings, board_id, now, age_threshold_seconds
            )
        except Exception:
            log.warning(
                "data_dir_gc: board=%r — closed-workspace prune failed",
                board_id,
                exc_info=True,
            )
            continue
    return total_removed


# ---------------------------------------------------------------------------
# Default-on GC: prune reproducible clones inside terminal-ticket workspaces
# ---------------------------------------------------------------------------

# Workspace subdirs holding reproducible git clones: the single-repo
# implement clone and the multi-repo (meta) clone tree. Everything else
# in the workspace (description.md, artifacts/, screenshots/) is a
# post-mortem record and is never touched by this GC.
_CLONE_SUBDIRS = ("repo", "repos")


def _remove_workspace_clones(board_id: str, ticket_id: str, ws_path: Path) -> int:
    """Delete the clone subdirs of one workspace; return dirs removed."""
    removed = 0
    for sub in _CLONE_SUBDIRS:
        clone = ws_path / sub
        if not clone.is_dir():
            continue
        shutil.rmtree(clone, ignore_errors=True)
        if not clone.exists():
            removed += 1
            log.info(
                "data_dir_gc: pruned terminal-ticket clone board=%r ticket=%s path=%s",
                board_id,
                ticket_id,
                clone,
            )
    return removed


def _prune_board_terminal_clones(
    settings: Settings,
    board_id: str,
    now: datetime,
    age_threshold_seconds: int,
) -> int:
    """Remove clone subdirs inside terminal-ticket workspaces for one board.

    Mirrors :func:`_prune_board_workspaces` but deletes only the
    ``repo/`` / ``repos/`` clone dirs, preserving the rest of the
    workspace. Returns the number of clone dirs removed.
    """
    workspaces_dir = settings.workspaces_dir_for(board_id)
    if not workspaces_dir.exists():
        return 0

    candidates = _workspace_candidates(workspaces_dir)
    if not candidates:
        return 0

    candidate_ids = [name for name, _ in candidates]
    terminal_ids, close_times = _terminal_close_times(settings, board_id, candidate_ids)

    removed = 0
    for name, path in candidates:
        if name not in terminal_ids:
            continue
        close_time = close_times.get(name) or _close_time_from_ticket_id(name)
        if close_time is None:
            continue
        if (now - close_time).total_seconds() < age_threshold_seconds:
            continue
        removed += _remove_workspace_clones(board_id, name, path)
    if removed:
        log.info(
            "data_dir_gc: board=%r pruned %d terminal-ticket clone(s)",
            board_id,
            removed,
        )
    return removed


def _prune_terminal_clones(settings: Settings) -> int:
    """Remove ``repo/`` / ``repos/`` clones from terminal-ticket
    workspaces across all boards. Returns the number of clone dirs
    removed."""
    now = _now()
    age_threshold_seconds = settings.data_dir_gc_prune_terminal_clones_age_seconds
    total_removed = 0
    for board_id in _boards_from_disk(settings):
        try:
            total_removed += _prune_board_terminal_clones(
                settings, board_id, now, age_threshold_seconds
            )
        except Exception:
            log.warning(
                "data_dir_gc: board=%r — terminal-clone prune failed",
                board_id,
                exc_info=True,
            )
            continue
    return total_removed


# ---------------------------------------------------------------------------
# Default-on GC: purge oldest archived DB rows (ticket spec "DB-maintenance")
# ---------------------------------------------------------------------------


def _has_active_child(settings: Settings, board_id: str, ticket_id: str) -> bool:
    """Return True if *ticket_id* has at least one child whose state is
    NOT in ``_TERMINAL_STATES``.

    Mirrors :meth:`_LifecycleMixin._has_active_child` from
    ``_lifecycle.py``, but operates as a module-level function so it can
    be called from the periodic data-dir audit pass without a service
    instance.
    """
    with db.session(settings, board_id) as s:
        stmt = (
            select(Ticket)
            .where(
                Ticket.parent_id == ticket_id,
                Ticket.state.notin_(list(_TERMINAL_STATES)),  # type: ignore[attr-defined]
            )
            .limit(1)
        )
        return s.exec(stmt).first() is not None


def _cascade_delete_ticket(settings: Settings, board_id: str, ticket_id: str) -> None:
    """Hard-delete *ticket_id* and its dependent rows in one transaction.

    Deletes ``TicketEvent`` and ``Comment`` rows referencing *ticket_id*,
    then the ``Ticket`` itself — mirrors ``TicketService.delete``.
    """
    with retry_on_db_full(settings, board_id) as s:
        for ev in s.exec(
            select(TicketEvent).where(TicketEvent.ticket_id == ticket_id)
        ).all():
            s.delete(ev)
        for c in s.exec(select(Comment).where(Comment.ticket_id == ticket_id)).all():
            s.delete(c)
        t = s.get(Ticket, ticket_id)
        if t is not None:
            s.delete(t)
        s.commit()


def _purge_board_archived_rows(
    settings: Settings, board_id: str, max_archived: int
) -> int:
    """Purge oldest terminal tickets from one board's ``mill.db``.

    Returns the number of tickets deleted for *board_id*.
    """
    # 1. Query terminal-state tickets, oldest first.
    with db.session(settings, board_id) as s:
        stmt = (
            select(Ticket)
            .where(Ticket.state.in_(_TERMINAL_STATES))  # type: ignore[attr-defined]
            .order_by(Ticket.created_at)  # type: ignore[arg-type]
        )
        candidates = list(s.exec(stmt).all())

    if len(candidates) <= max_archived:
        return 0

    excess = len(candidates) - max_archived
    deleted = 0

    for ticket in candidates:
        if deleted >= excess:
            break
        # Skip terminal tickets that have active children.
        if _has_active_child(settings, board_id, ticket.id):
            continue

        _cascade_delete_ticket(settings, board_id, ticket.id)
        deleted += 1
        log.info(
            "data_dir_gc: purged archived ticket board=%r ticket=%s",
            board_id,
            ticket.id,
        )

    # 2. Reclaim disk space freed by the deletes.
    if deleted:
        with retry_on_db_full(settings, board_id) as s:
            s.execute(text("VACUUM"))
            s.commit()
        log.info(
            "data_dir_gc: board=%r — VACUUM complete "
            "after purging %d terminal ticket(s)",
            board_id,
            deleted,
        )

    return deleted


def _prune_archived_db_rows(settings: Settings) -> int:
    """Purge oldest terminal-ticket rows from every board's ``mill.db``.

    Iterates every board on disk and delegates to
    :func:`_purge_board_archived_rows`.  Returns the total number of
    tickets deleted across all boards.

    Per-board exceptions are logged at WARNING level and the pass
    continues to the next board.
    """
    max_archived = settings.max_archived_tickets
    if max_archived <= 0:
        return 0

    total_deleted = 0
    for board_id in _boards_from_disk(settings):
        try:
            total_deleted += _purge_board_archived_rows(
                settings, board_id, max_archived
            )
        except Exception:
            log.warning(
                "data_dir_gc: board=%r — archived DB row purge failed",
                board_id,
                exc_info=True,
            )
            continue

    return total_deleted


# ---------------------------------------------------------------------------
# Default-on GC: prune orphan workspace directories
# ---------------------------------------------------------------------------


def _prune_board_orphan_workspaces(
    settings: Settings,
    board_id: str,
    now: datetime,
    age_threshold_seconds: int,
) -> int:
    """Remove orphan workspace directories for one board.

    Calls :func:`find_orphan_workspaces` to discover directories whose
    ticket is absent from the DB, then age-guards each via
    :func:`_close_time_from_ticket_id` (orphans have no DB close time).
    Returns the number of dirs removed.
    """
    orphans = find_orphan_workspaces(settings, board_id)
    if not orphans:
        return 0

    removed = 0
    for orphan in orphans:
        close_time = _close_time_from_ticket_id(orphan.ticket_id)
        if close_time is None:
            continue
        if (now - close_time).total_seconds() < age_threshold_seconds:
            continue
        shutil.rmtree(orphan.path, ignore_errors=True)
        if not orphan.path.exists():
            removed += 1
            log.info(
                "data_dir_gc: pruned orphan workspace board=%r ticket=%s path=%s",
                board_id,
                orphan.ticket_id,
                orphan.path,
            )
    if removed:
        log.info(
            "data_dir_gc: board=%r pruned %d orphan workspace(s)",
            board_id,
            removed,
        )
    return removed


def _prune_orphan_workspaces(settings: Settings) -> int:
    """Remove orphan workspace directories older than the configured age.

    Iterates every board on disk, delegates to
    :func:`_prune_board_orphan_workspaces`.  Returns the total number
    of directories removed across all boards.

    The knob ``settings.data_dir_gc_prune_orphans`` is NOT checked
    here — the call site must guard the invocation.
    """
    now = _now()
    age_threshold_seconds = settings.data_dir_gc_prune_orphans_age_seconds
    total_removed = 0
    for board_id in _boards_from_disk(settings):
        try:
            total_removed += _prune_board_orphan_workspaces(
                settings, board_id, now, age_threshold_seconds
            )
        except Exception:
            log.warning(
                "data_dir_gc: board=%r — orphan workspace prune failed",
                board_id,
                exc_info=True,
            )
            continue
    return total_removed


# ---------------------------------------------------------------------------
# Default-on GC: truncate over-cap *_memory.md files on disk
# ---------------------------------------------------------------------------

# tail_keep prepends a "[… memory truncated: N chars omitted]" note when it
# truncates.  The note itself can push the file a few dozen chars past the
# nominal cap.  Reserve a conservative buffer so the final file reliably
# fits within max_memory_chars even with the note.
_NOTE_OVERHEAD = 80


def _prune_oversized_memory_ledgers(settings: Settings) -> int:
    """Truncate over-cap ``*_memory.md`` files under ``data_dir`` in place.

    Reuses :func:`persist_memory` from ``pass_runner`` — the same
    ``tail_keep`` primitive the agent already uses at read/write time —
    so the on-disk result is byte-for-byte the tail the agent would
    have seen anyway.  A small buffer (``_NOTE_OVERHEAD``) is
    subtracted from the target to compensate for the truncation-note
    overhead.

    Returns the number of files actually truncated.
    """
    if settings.max_memory_chars <= 0:
        return 0

    data_dir = settings.data_dir
    if not data_dir.is_dir():
        return 0

    # Lazy import to avoid circular dependency at module load time.
    from ..pass_runner import persist_memory  # noqa: PLC0415

    target_chars = max(1, settings.max_memory_chars - _NOTE_OVERHEAD)
    truncated = 0

    # Memory ledgers live at <data_dir>/<board>/<name>_memory.md (and the
    # legacy board-less <data_dir>/<name>_memory.md). Glob only those two
    # depths instead of rglob("**"), which would recurse into every ticket
    # workspace and git clone under the data dir — tens of thousands of files
    # walked just to find a handful of board-level ledgers, on every audit
    # pass. (Same motivation as the O(files×depth) finders fix.)
    mem_files = sorted(
        set(data_dir.glob("*_memory.md")) | set(data_dir.glob("*/*_memory.md"))
    )
    for mem_file in mem_files:
        if not mem_file.is_file():
            continue
        try:
            text = mem_file.read_text(encoding="utf-8")
        except OSError:
            log.warning(
                "data_dir_gc: cannot read memory ledger %s — skipping",
                mem_file,
            )
            continue
        # Compare encoded byte size, not char count — the unbounded-
        # candidate check in finders.py compares st_size (bytes)
        # against the same cap, so a file whose character count fits
        # within max_memory_chars but whose UTF-8 byte count does not
        # (e.g. due to multi-byte characters) would otherwise be
        # skipped here and produce a false-positive unbounded finding.
        if len(text.encode("utf-8")) <= settings.max_memory_chars:
            continue
        # persist_memory handles its own OSError on write internally
        # (logs and returns); no additional guard needed.
        persist_memory(mem_file, text, max_chars=target_chars)
        truncated += 1

    if truncated:
        log.info(
            "data_dir_gc: truncated %d oversized memory ledger(s)",
            truncated,
        )
    return truncated
