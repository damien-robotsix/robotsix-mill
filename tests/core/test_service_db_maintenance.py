"""Periodic DB-maintenance tests extracted from test_service.py.

Covers ``db_maintenance_pass``: per-ticket event cap, archived-ticket
purge, per-ticket comment cap, PRAGMA optimize / WAL checkpoint, and
orphan reconciliation.

Shared helpers (``_close_ticket``, ``_terminal_count``, ``_comment_count``,
``_get_comment``) are imported from test_service.py, matching the pattern
used by the other extracted test_service_*.py modules.
"""

from datetime import UTC

from robotsix_mill.core.states import State
from tests.core.test_service import (
    _close_ticket,
    _comment_count,
    _get_comment,
    _terminal_count,
)

# ---------------------------------------------------------------------------
# DB maintenance pass
# ---------------------------------------------------------------------------


class TestDbMaintenancePass:
    """Tests for periodic DB maintenance: event cap + archive purge +
    PRAGMA optimize + WAL checkpoint."""

    def test_empty_db_returns_zero_summary(self, service):
        """db_maintenance_pass on an empty DB returns all-zero summary
        and does not error."""
        summary = service.db_maintenance_pass()
        assert summary == {
            "drafts_auto_closed": 0,
            "orphans_reconciled": 0,
            "archived_purged": 0,
            "events_pruned": 0,
            "comments_pruned": 0,
            "tickets_pruned": 0,
        }

    def test_event_cap_prunes_excess(self, service, settings):
        """After accumulating > max_events_per_ticket events on a
        non-terminal ticket, only the most recent max_events_per_ticket
        remain, and the earliest remaining event has prev_hash=None."""
        settings.max_events_per_ticket = 5
        t = service.create("event-cap test")

        # Insert 10 same-state events via add_step_event.
        for i in range(10):
            service.add_step_event(t.id, f"step {i}")

        # Verify 11 events before maintenance (1 created + 10 steps).
        assert len(service.history(t.id)) == 11

        summary = service.db_maintenance_pass()
        assert summary["events_pruned"] == 6  # 11 - 5 = 6
        assert summary["tickets_pruned"] == 1

        # After pruning, 5 events remain.
        hist = service.history(t.id)
        assert len(hist) == 5

        # The earliest remaining event must have prev_hash=None.
        assert hist[0].prev_hash is None

        # The ticket itself must still exist.
        assert service.get(t.id) is not None

    def test_max_events_zero_disables_event_cap(self, service, settings):
        """Setting max_events_per_ticket=0 disables per-ticket event
        capping."""
        settings.max_events_per_ticket = 0
        t = service.create("no-cap test")

        for i in range(50):
            service.add_step_event(t.id, f"step {i}")

        summary = service.db_maintenance_pass()
        assert summary["events_pruned"] == 0
        assert summary["tickets_pruned"] == 0
        assert len(service.history(t.id)) == 51  # all remain

    def test_archive_purge_runs_during_maintenance_pass(self, service, settings):
        """db_maintenance_pass calls _maybe_purge_archived, so terminal
        tickets beyond max_archived_tickets are purged even when no
        ticket has recently transitioned to a terminal state."""
        settings.max_archived_tickets = 2

        # Create 4 terminal tickets without triggering the per-transition
        # purge (we'll close them while cap is high, then lower it).
        settings.max_archived_tickets = 100
        tickets = []
        for i in range(4):
            t = service.create(f"purge test {i}")
            _close_ticket(service, t)
            tickets.append(t)

        # Now lower the cap and run maintenance.
        settings.max_archived_tickets = 2
        summary = service.db_maintenance_pass()

        assert summary["archived_purged"] == 2

        # Only 2 terminal tickets remain (the two newest).
        assert _terminal_count(service) == 2
        # Oldest two should be gone.
        assert service.get(tickets[0].id) is None
        assert service.get(tickets[1].id) is None
        assert service.get(tickets[2].id) is not None
        assert service.get(tickets[3].id) is not None

    def test_non_terminal_not_deleted(self, service, settings):
        """The event cap only prunes TicketEvent rows; it never deletes
        the Ticket row itself."""
        settings.max_events_per_ticket = 2
        t = service.create("keep-alive test")

        for i in range(10):
            service.add_step_event(t.id, f"step {i}")

        summary = service.db_maintenance_pass()
        assert summary["events_pruned"] > 0
        assert service.get(t.id) is not None
        assert service.get(t.id).state == State.DRAFT

    # --- comment cap tests ---

    def test_comment_cap_prunes_excess(self, service, settings):
        """After accumulating > max_comments_per_ticket comments on a
        non-terminal ticket, only the most recent max_comments_per_ticket
        remain (when all are unprotected)."""
        settings.max_comments_per_ticket = 5
        t = service.create("comment-cap test")

        # Add 10 comments (all closed so they're unprotected).
        for i in range(10):
            c = service.add_comment(t.id, f"comment {i}")
            # Close it via close_thread so it's unprotected.
            service.close_thread(c.id, ticket_id=t.id)

        # Verify 10 comments before maintenance.
        assert _comment_count(service, t.id) == 10

        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 5  # 10 - 5 = 5

        # After pruning, 5 comments remain.
        remaining = _comment_count(service, t.id)
        assert remaining == 5

        # The ticket itself must still exist.
        assert service.get(t.id) is not None

    def test_max_comments_zero_disables_comment_cap(self, service, settings):
        """Setting max_comments_per_ticket=0 disables per-ticket comment
        capping."""
        settings.max_comments_per_ticket = 0
        t = service.create("no-cap test")

        for i in range(50):
            c = service.add_comment(t.id, f"comment {i}")
            service.close_thread(c.id, ticket_id=t.id)

        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 0
        assert _comment_count(service, t.id) == 50  # all remain

    def test_comment_cap_under_limit_noop(self, service, settings):
        """When a ticket has fewer comments than the cap, nothing is pruned."""
        settings.max_comments_per_ticket = 20
        t = service.create("under-cap test")

        c = service.add_comment(t.id, "only comment")
        service.close_thread(c.id, ticket_id=t.id)

        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 0
        assert _comment_count(service, t.id) == 1

    def test_comment_cap_protects_open_thread(self, service, settings):
        """An open top-level thread (closed_at IS NULL) and its replies
        are never pruned, even when the ticket exceeds the cap."""
        from datetime import datetime

        from robotsix_mill.core import db
        from robotsix_mill.core.models import Comment

        settings.max_comments_per_ticket = 3
        t = service.create("open-thread test")

        # Create an open [ASK_USER] thread.
        ask = service.add_comment(t.id, "[ASK_USER] a question?", author="agent")
        # Add a reply to it.
        service.add_comment(t.id, "reply to ask", author="user", parent_id=ask.id)

        # Pile on many closed comments (oldest first).
        for i in range(10):
            c = service.add_comment(t.id, f"old closed {i}")
            # Close it so it's unprotected.
            with db.session(settings, service._board_for(t.id)) as s:
                cmt = s.get(Comment, c.id)
                cmt.closed_at = datetime.now(UTC)
                s.add(cmt)
                s.commit()

        # Before maintenance: 1 ask + 1 reply + 10 closed = 12 comments.
        assert _comment_count(service, t.id) == 12

        summary = service.db_maintenance_pass()
        # Cap is 3, we have 12 total. The ask + reply (2 protected) +
        # up to 1 unprotected = effective cap is 3. So we delete oldest
        # closed until total <= cap or no more unprotected.
        # We can only delete 9 of 10 closed (need to leave 1 to reach cap
        # of 3 with the 2 protected). So 9 deleted.
        assert summary["comments_pruned"] == 9

        remaining = _comment_count(service, t.id)
        assert remaining == 3  # ask + reply + 1 most-recent closed

        # The open ask thread and its reply must survive.
        assert _get_comment(service, ask.id) is not None
        assert _get_comment(service, ask.id).closed_at is None  # still open

    def test_comment_cap_resets_orphaned_parent_id(self, service, settings):
        """When a parent comment is deleted, surviving replies have their
        parent_id reset to None."""
        from datetime import datetime

        from robotsix_mill.core import db
        from robotsix_mill.core.models import Comment

        settings.max_comments_per_ticket = 3
        t = service.create("orphan-reset test")

        # Create the parent FIRST (oldest unprotected).
        parent = service.add_comment(t.id, "closed thread")

        # Add 5 filler closed comments between the parent and its reply,
        # so the reply is the newest comment and survives the prune while
        # the parent (oldest unprotected) is deleted.
        filler_ids: list[int] = []
        for i in range(5):
            c = service.add_comment(t.id, f"filler {i}")
            filler_ids.append(c.id)

        # Create the reply LAST (newest), pointing at the old parent.
        reply = service.add_comment(t.id, "reply", parent_id=parent.id)

        # Close all comments so they're all unprotected.
        with db.session(settings, service._board_for(t.id)) as s:
            for cid in [parent.id, reply.id, *filler_ids]:
                cmt = s.get(Comment, cid)
                cmt.closed_at = datetime.now(UTC)
                s.add(cmt)
            s.commit()

        # 7 comments total (parent + 5 fillers + reply), cap=3.
        # 4 oldest unprotected pruned: parent + filler 0-2.
        # Reply (id=7) survives with parent deleted → parent_id → None.
        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 4

        # The reply must survive and its parent_id must be reset to None.
        r = _get_comment(service, reply.id)
        assert r is not None, "reply should survive the prune"
        assert r.parent_id is None, "orphaned reply must have parent_id reset"

    def test_comment_cap_empty_db(self, service, settings):
        """db_maintenance_pass on an empty DB returns zero comments_pruned."""
        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 0

    def test_comment_cap_terminal_ticket_not_capped(self, service, settings):
        """Comments on terminal (archived) tickets are NOT capped by
        db_maintenance_pass — they are reclaimed by the archive purge."""
        settings.max_comments_per_ticket = 2
        t = service.create("terminal test")

        # Add 10 comments.
        for i in range(10):
            service.add_comment(t.id, f"comment {i}")

        # Close the ticket to a terminal state.
        _close_ticket(service, t)

        # Run maintenance. The ticket is terminal, so the comment cap
        # loop only looks at non-terminal tickets. Comments should survive.
        summary = service.db_maintenance_pass()
        assert summary["comments_pruned"] == 0
        # All 10 comments remain (they'll be cascade-deleted when the
        # archive purge fires).
        assert _comment_count(service, t.id) == 10

    def test_pragma_optimize_runs(self, service, settings, monkeypatch):
        """db_maintenance_pass issues PRAGMA optimize after cleanup."""

        from sqlalchemy import Connection

        pragmas_seen = []

        # Spy on exec_driver_sql to capture PRAGMA optimize.
        _orig_eds = Connection.exec_driver_sql

        def _spy_eds(conn_self, statement, *args, **kwargs):
            try:
                stmt_str = str(statement)
                if "pragma" in stmt_str.lower():
                    pragmas_seen.append(stmt_str)
            except Exception:
                pass
            return _orig_eds(conn_self, statement, *args, **kwargs)

        monkeypatch.setattr(Connection, "exec_driver_sql", _spy_eds)

        service.db_maintenance_pass()

        assert any("optimize" in s.lower() for s in pragmas_seen), (
            f"PRAGMA optimize was not observed; saw: {pragmas_seen}"
        )

    def test_wal_truncation_after_maintenance_pass(self, service, settings):
        """db_maintenance_pass issues PRAGMA wal_checkpoint(TRUNCATE)
        and the WAL file is truncated to near-zero bytes afterward."""
        from robotsix_mill.core import db as db_mod

        # Locate the DB path and derive the WAL path.
        db_path = db_mod._db_path(settings, service.board_id)
        wal_path = db_path.with_suffix(db_path.suffix + "-wal")

        # Create a ticket and generate enough writes to grow the WAL.
        t = service.create("wal-truncation test")
        for i in range(50):
            service.add_step_event(t.id, f"wal event {i}")

        # The WAL file should exist and be non-trivial.
        # (On first access with WAL mode, SQLite creates the WAL file.)
        wal_size_before = wal_path.stat().st_size if wal_path.exists() else 0

        # Run the maintenance pass, which now includes the TRUNCATE checkpoint.
        summary = service.db_maintenance_pass()

        # After the pass the WAL should be truncated to ~0 bytes or absent.
        wal_size_after = wal_path.stat().st_size if wal_path.exists() else 0

        # If the WAL was non-zero before, assert it shrank.  When the
        # file never existed (size 0 before and after) the test still
        # passes — the checkpoint call completed without error, and the
        # assertion only fails when a non-zero WAL fails to shrink.
        assert wal_size_after <= wal_size_before, (
            f"WAL file grew after TRUNCATE checkpoint: "
            f"{wal_size_before} → {wal_size_after}"
        )
        if wal_size_before > 0:
            assert wal_size_after < 100, (
                f"WAL file not truncated: {wal_size_after} bytes remain"
            )

        # Spot-check: the summary contract is unchanged.
        assert set(summary.keys()) == {
            "drafts_auto_closed",
            "orphans_reconciled",
            "archived_purged",
            "events_pruned",
            "comments_pruned",
            "tickets_pruned",
        }

    def test_reconcile_orphan_cards_purges_rows_with_missing_ticket(
        self, service, settings, caplog
    ):
        """A card whose Ticket row is gone but whose event/comment rows
        linger (an interrupted delete / out-of-band removal) is the exact
        shape that makes ``/board/cards`` list an id which 404s on
        ``GET /tickets/{id}``.  The maintenance reconcile logs each such
        id and purges the dangling rows so the two views agree."""
        import logging

        from sqlmodel import select

        from robotsix_mill.core import db
        from robotsix_mill.core.models import Comment, Ticket, TicketEvent

        # Orphan ticket: has history + a comment, then its row vanishes.
        orphan = service.create("orphan card")
        service.add_step_event(orphan.id, "work step")
        service.add_comment(orphan.id, "a comment")

        # Healthy ticket that must survive the reconcile untouched.
        healthy = service.create("healthy card")
        service.add_step_event(healthy.id, "healthy step")

        # Simulate the divergence: delete ONLY the Ticket row, leaving
        # its event/comment rows dangling (a card with no ticket store row).
        with db.session(settings, service.board_id) as s:
            s.delete(s.get(Ticket, orphan.id))
            s.commit()

        # Pre-condition: the id 404s (get() is what /tickets/{id} uses)
        # yet its event rows still reference it.
        assert service.get(orphan.id) is None
        with db.session(settings, service.board_id) as s:
            assert (
                s.exec(
                    select(TicketEvent.ticket_id).where(
                        TicketEvent.ticket_id == orphan.id
                    )
                ).first()
                is not None
            )

        with caplog.at_level(logging.WARNING, logger="robotsix_mill.service"):
            summary = service.db_maintenance_pass()
        assert summary["orphans_reconciled"] == 1
        assert any(orphan.id in r.getMessage() for r in caplog.records)

        # The dangling event/comment rows are gone...
        with db.session(settings, service.board_id) as s:
            assert (
                s.exec(
                    select(TicketEvent.ticket_id).where(
                        TicketEvent.ticket_id == orphan.id
                    )
                ).first()
                is None
            )
            assert (
                s.exec(
                    select(Comment.ticket_id).where(Comment.ticket_id == orphan.id)
                ).first()
                is None
            )

        # ...and the healthy ticket plus its history are untouched.
        assert service.get(healthy.id) is not None
        assert len(service.history(healthy.id)) >= 2

        # Idempotent: a second pass finds nothing left to reconcile.
        assert service.db_maintenance_pass()["orphans_reconciled"] == 0
