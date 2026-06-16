"""Orphan-workspace detection and workspace/clone GC (ticket 5).

Detects workspace directories whose ticket no longer exists in the DB,
and provides opt-in closed-workspace pruning plus default-on
terminal-clone GC.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlmodel import select

from ...config import Settings
from ...core import db
from ...core.models import Comment, ProposedAction, Ticket, TicketEvent, _now

from .growth import _TICKET_ID_PREFIX_RE, _BATCH_SIZE, _TERMINAL_STATES

log = logging.getLogger("robotsix_mill.data_dir_audit")


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
                "data_dir_audit: pruned closed workspace board=%r ticket=%s path=%s",
                board_id,
                name,
                path,
            )
    if removed:
        log.info(
            "data_dir_audit: board=%r pruned %d closed workspace(s)",
            board_id,
            removed,
        )
    return removed


def _prune_closed_workspaces(settings: Settings) -> int:
    """Remove workspace dirs of terminal-state tickets older than the
    configured age. Returns the number of directories removed."""
    now = _now()
    age_threshold_seconds = settings.data_dir_audit_prune_closed_age_seconds
    total_removed = 0
    for board_id in _boards_from_disk(settings):
        try:
            total_removed += _prune_board_workspaces(
                settings, board_id, now, age_threshold_seconds
            )
        except Exception:
            log.warning(
                "data_dir_audit: board=%r — closed-workspace prune failed",
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
                "data_dir_audit: pruned terminal-ticket clone "
                "board=%r ticket=%s path=%s",
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
            "data_dir_audit: board=%r pruned %d terminal-ticket clone(s)",
            board_id,
            removed,
        )
    return removed


def _prune_terminal_clones(settings: Settings) -> int:
    """Remove ``repo/`` / ``repos/`` clones from terminal-ticket
    workspaces across all boards. Returns the number of clone dirs
    removed."""
    now = _now()
    age_threshold_seconds = settings.data_dir_audit_prune_terminal_clones_age_seconds
    total_removed = 0
    for board_id in _boards_from_disk(settings):
        try:
            total_removed += _prune_board_terminal_clones(
                settings, board_id, now, age_threshold_seconds
            )
        except Exception:
            log.warning(
                "data_dir_audit: board=%r — terminal-clone prune failed",
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
                Ticket.state.notin_(list(_TERMINAL_STATES)),
            )
            .limit(1)
        )
        return s.exec(stmt).first() is not None


def _prune_archived_db_rows(settings: Settings) -> int:
    """Purge oldest terminal-ticket rows from every board's ``mill.db``.

    Iterates every board on disk, queries tickets in terminal states
    (``CLOSED``, ``ANSWERED``, ``EPIC_CLOSED``) ordered by
    ``created_at`` ASC, and hard-deletes the oldest excess when the
    count exceeds ``settings.max_archived_tickets``. Tickets that are
    parents of at least one child in a non-terminal state are skipped
    (mirroring the ``_has_active_child`` guard in
    ``_maybe_purge_archived``).

    After all candidate deletes for a board, a ``VACUUM`` is run to
    reclaim disk space from the freed pages.  Returns the total number
    of tickets deleted across all boards.

    Per-board exceptions are logged at WARNING level and the pass
    continues to the next board.
    """
    max_archived = settings.max_archived_tickets
    if max_archived <= 0:
        return 0

    total_deleted = 0
    for board_id in _boards_from_disk(settings):
        try:
            # 1. Query terminal-state tickets, oldest first.
            with db.session(settings, board_id) as s:
                stmt = (
                    select(Ticket)
                    .where(Ticket.state.in_(_TERMINAL_STATES))
                    .order_by(Ticket.created_at)
                )
                candidates = list(s.exec(stmt).all())

            if len(candidates) <= max_archived:
                continue

            excess = len(candidates) - max_archived
            deleted = 0

            for ticket in candidates:
                if deleted >= excess:
                    break
                # Skip terminal tickets that have active children.
                if _has_active_child(settings, board_id, ticket.id):
                    continue

                # Cascade-delete: events, proposed actions, comments,
                # then the ticket itself — one transaction per ticket
                # (mirrors TicketService.delete).
                with db.session(settings, board_id) as s:
                    for ev in s.exec(
                        select(TicketEvent).where(
                            TicketEvent.ticket_id == ticket.id
                        )
                    ).all():
                        s.delete(ev)
                    for pa in s.exec(
                        select(ProposedAction).where(
                            ProposedAction.target_ticket_id == ticket.id
                        )
                    ).all():
                        s.delete(pa)
                    for c in s.exec(
                        select(Comment).where(
                            Comment.ticket_id == ticket.id
                        )
                    ).all():
                        s.delete(c)
                    t = s.get(Ticket, ticket.id)
                    if t is not None:
                        s.delete(t)
                    s.commit()

                deleted += 1
                log.info(
                    "data_dir_audit: purged archived ticket board=%r ticket=%s",
                    board_id,
                    ticket.id,
                )

            total_deleted += deleted

            # 2. Reclaim disk space freed by the deletes.
            if deleted:
                with db.session(settings, board_id) as s:
                    s.exec(text("VACUUM"))
                    s.commit()
                log.info(
                    "data_dir_audit: board=%r — VACUUM complete "
                    "after purging %d terminal ticket(s)",
                    board_id,
                    deleted,
                )

        except Exception:
            log.warning(
                "data_dir_audit: board=%r — archived DB row purge failed",
                board_id,
                exc_info=True,
            )
            continue

    return total_deleted


# ---------------------------------------------------------------------------
# Runner helper — scans orphans across all boards
# ---------------------------------------------------------------------------


def _scan_orphan_workspaces(
    settings: Settings,
) -> tuple[dict[str, list[OrphanWorkspace]], int]:
    """Scan every board with a ``mill.db`` for orphan workspaces.

    Returns ``(orphans_by_board, total_orphans)``. Per-board failures
    are logged and skipped (the pass continues across boards).
    """
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
        if not found:
            continue
        orphans_by_board[board_id] = found
        total_orphans += len(found)
        for o in found:
            log.info(
                "data_dir_audit: orphan workspace board=%r ticket=%s path=%s size=%dB",
                board_id,
                o.ticket_id,
                o.path,
                o.dir_size_bytes,
            )
    return orphans_by_board, total_orphans
