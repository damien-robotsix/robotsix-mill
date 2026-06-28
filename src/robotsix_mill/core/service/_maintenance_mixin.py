"""DB-maintenance surface of :class:`TicketService` (``_MaintenanceMixin``)."""

from __future__ import annotations

import logging
from typing import cast

from sqlmodel import col, select

from .. import db
from ..models import (
    Comment,
    Ticket,
    TicketEvent,
)
from ._base import _ServiceBase

log = logging.getLogger("robotsix_mill.service")


class _MaintenanceMixin(_ServiceBase):
    """Archive purge, per-ticket event/comment caps, and DB optimize."""

    def _maybe_purge_archived(self) -> None:
        """Purge oldest terminal tickets when the cap is exceeded.

        Reads ``max_archived_tickets`` from settings.  If <= 0 the
        purge is disabled.  Queries all tickets in ``_ARCHIVABLE_STATES``
        ordered by ``created_at`` ascending and deletes the oldest until
        the count is within the cap — but skips any terminal ticket that
        is the parent of at least one child in a non-archivable state.
        """
        max_archived = self.settings.max_archived_tickets
        if max_archived <= 0:
            return

        with db.session(self.settings, self.board_id) as s:
            stmt = (
                select(Ticket)
                .where(Ticket.state.in_(list(self._ARCHIVABLE_STATES)))
                .order_by(Ticket.created_at)
            )
            candidates = list(s.exec(stmt).all())

        if len(candidates) <= max_archived:
            return

        excess = len(candidates) - max_archived
        deleted = 0
        for ticket in candidates:
            if deleted >= excess:
                break
            # Skip if this terminal ticket is the parent of any
            # child still in a non-archivable (active) state.
            if self._has_active_child(ticket.id):
                continue
            self.delete(ticket.id)
            deleted += 1

    def _maybe_purge_ticket_events(self, ticket_id: str) -> int:
        """Prune oldest TicketEvent rows for *ticket_id* when the count
        exceeds ``max_events_per_ticket``, keeping only the most recent.

        After deletion, sets ``prev_hash = None`` on the new earliest
        remaining event so the hash chain starts cleanly at the prune
        point.  Returns the number of rows deleted (0 when under cap
        or when the cap is disabled).
        """
        max_events = self.settings.max_events_per_ticket
        if max_events <= 0:
            return 0

        with db.session(self.settings, self.board_id) as s:
            all_events = s.exec(
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket_id)
                .order_by(col(TicketEvent.id))
            ).all()

            total = len(all_events)
            if total <= max_events:
                return 0

            excess = total - max_events
            # Delete the oldest *excess* events.
            for ev in all_events[:excess]:
                s.delete(ev)

            # Reset prev_hash on the new earliest remaining event.
            earliest = all_events[excess] if excess < len(all_events) else None
            if earliest is not None and earliest.prev_hash is not None:
                earliest.prev_hash = None
                s.add(earliest)

            s.commit()
            return excess

    def _maybe_purge_ticket_comments(self, ticket_id: str) -> int:
        """Prune oldest unprotected Comment rows for *ticket_id* when the
        count exceeds ``max_comments_per_ticket``, keeping only the most
        recent.

        OPEN threads (top-level comments with ``closed_at IS NULL``) and
        their replies are **protected** — never deleted — so
        ``[ASK_USER]`` auto-resume and active discussions are preserved
        even when the ticket exceeds the cap.

        After deletions, any surviving reply whose ``parent_id``
        references a deleted comment has its ``parent_id`` reset to
        ``None``, mirroring the ``prev_hash`` reset in
        ``_maybe_purge_ticket_events``.

        Returns the number of rows deleted (0 when under cap, when the
        cap is disabled, or when there are no unprotected comments).
        """
        max_comments = self.settings.max_comments_per_ticket
        if max_comments <= 0:
            return 0

        with db.session(self.settings, self.board_id) as s:
            all_comments = s.exec(
                select(Comment)
                .where(Comment.ticket_id == ticket_id)
                .order_by(col(Comment.id))
            ).all()

            total = len(all_comments)
            if total <= max_comments:
                return 0

            # --- protected set: open threads and their replies ---
            # An "open thread" is a top-level comment (parent_id IS NULL)
            # whose closed_at IS NULL.  Every reply (parent_id IS NOT NULL)
            # whose top-level ancestor is open is also protected.
            open_root_ids: set[int] = set()
            for c in all_comments:
                cid = cast(int, c.id)  # DB-loaded comment, id is never None
                if c.parent_id is None and c.closed_at is None:
                    open_root_ids.add(cid)

            protected_ids: set[int] = set()
            for c in all_comments:
                cid = cast(int, c.id)  # DB-loaded comment, id is never None
                if cid in open_root_ids:
                    protected_ids.add(cid)
                    continue
                if c.parent_id is not None:
                    # Walk up to find the root ancestor.
                    ancestor_pid: int | None = c.parent_id
                    # Guard against cycles (should never exist).
                    seen: set[int] = {cid}
                    while ancestor_pid is not None:
                        if ancestor_pid in seen:
                            break
                        if ancestor_pid in open_root_ids:
                            protected_ids.add(cid)
                            break
                        seen.add(ancestor_pid)
                        # Find the parent comment in the loaded list.
                        parent = next(
                            (x for x in all_comments if x.id == ancestor_pid), None
                        )
                        ancestor_pid = parent.parent_id if parent else None

            # --- delete oldest unprotected excess ---
            unprotected = [c for c in all_comments if c.id not in protected_ids]
            excess = total - max_comments
            deleted_ids: set[int] = set()
            deleted = 0
            for c in unprotected:
                if deleted >= excess:
                    break
                cid = cast(int, c.id)  # DB-loaded comment, id is never None
                s.delete(c)
                deleted_ids.add(cid)
                deleted += 1

            # --- reset parent_id on surviving replies that referenced
            # a now-deleted comment ---
            if deleted_ids:
                for c in all_comments:
                    if c.id not in deleted_ids and c.parent_id in deleted_ids:
                        c.parent_id = None
                        s.add(c)

            s.commit()
            return deleted

    def db_maintenance_pass(self) -> dict[str, int]:
        """Run one DB maintenance sweep: archive purge, per-ticket event
        cap, and SQLite ``PRAGMA optimize``.

        Returns a summary dict with keys ``archived_purged``,
        ``events_pruned``, ``comments_pruned``, and ``tickets_pruned``.
        """
        result: dict[str, int] = {
            "archived_purged": 0,
            "events_pruned": 0,
            "comments_pruned": 0,
            "tickets_pruned": 0,
        }

        # 1. Count terminal tickets before purge, then run it.
        with db.session(self.settings, self.board_id) as s:
            before = s.exec(
                select(Ticket).where(
                    col(Ticket.state).in_(list(self._ARCHIVABLE_STATES))
                )
            ).all()
        before_count = len(before)
        self._maybe_purge_archived()
        with db.session(self.settings, self.board_id) as s:
            after = s.exec(
                select(Ticket).where(
                    col(Ticket.state).in_(list(self._ARCHIVABLE_STATES))
                )
            ).all()
        result["archived_purged"] = before_count - len(after)

        # 2. Event cap for ALL non-terminal tickets.
        with db.session(self.settings, self.board_id) as s:
            active_ids = s.exec(
                select(Ticket.id).where(
                    col(Ticket.state).notin_(list(self._ARCHIVABLE_STATES))
                )
            ).all()
        for tid in active_ids:
            pruned = self._maybe_purge_ticket_events(tid)
            if pruned:
                result["events_pruned"] += pruned
                result["tickets_pruned"] += 1
            pruned_c = self._maybe_purge_ticket_comments(tid)
            if pruned_c:
                result["comments_pruned"] += pruned_c

        # 3. Reclaim freed pages and truncate the WAL file.
        with db.session(self.settings, self.board_id) as s:
            s.connection().exec_driver_sql("PRAGMA optimize")
            s.connection().exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            s.commit()

        return result
