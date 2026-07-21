"""Diagnostic-event surface of :class:`TicketService` (``_DiagnosticMixin``).

Emitting, querying, and aggregating :class:`~.models.DiagnosticEvent`
rows — the data plane for the diagnostic-event → recurring-category →
fix-proposal pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlmodel import func, select

from .. import db
from ..db import retry_on_db_full
from ..models import DiagnosticEvent
from ._base import _ServiceBase

log = logging.getLogger("robotsix_mill.service")


class _DiagnosticMixin(_ServiceBase):
    """Diagnostic-event emission, query, and recurring-category aggregation."""

    def emit_diagnostic_event(
        self,
        ticket_id: str,
        category: str,
        sub_category: str,
        reason: str,
        *,
        repo_id: str = "",
    ) -> DiagnosticEvent | None:
        """Create a :class:`DiagnosticEvent`, deduped on *ticket_id* + *sub_category*.

        When a matching event already exists for the same *ticket_id* and
        *sub_category*, the call is silently skipped (returns ``None``) so
        retries of the same failure on the same ticket don't flood the
        category.

        Returns the new event on success, or ``None`` when deduped.
        """
        board = self._board_for(ticket_id)
        with retry_on_db_full(self.settings, board) as s:
            # Dedup: same ticket_id + same sub_category → skip.
            existing = s.exec(
                select(DiagnosticEvent)
                .where(
                    DiagnosticEvent.ticket_id == ticket_id,
                    DiagnosticEvent.sub_category == sub_category,
                )
                .limit(1)
            ).first()
            if existing is not None:
                return None

            event = DiagnosticEvent(
                ticket_id=ticket_id,
                repo_id=repo_id or self.board_id,
                category=category,
                sub_category=sub_category,
                reason=reason,
            )
            s.add(event)
            s.commit()
            s.refresh(event)
            return event

    def list_diagnostic_events(
        self,
        *,
        category: str | None = None,
        ticket_id: str | None = None,
        repo_id: str | None = None,
        sub_category: str | None = None,
        limit: int = 200,
    ) -> list[DiagnosticEvent]:
        """Query diagnostic events with optional filters.

        When *ticket_id* is provided the lookup uses the ticket's board
        (via ``_board_for``).  When only *repo_id* / *category* /
        *sub_category* filters are given the query runs against
        ``self.board_id`` (the service's own board).  Cross-board
        fanout is not yet implemented for the filter-only path — callers
        needing a cross-board aggregation should iterate across boards
        themselves.

        Args:
            category: Filter by exact category (e.g. ``"CI_FAILURE"``).
            ticket_id: Filter to events for a single ticket.
            repo_id: Filter by board id.
            sub_category: Filter by exact sub-category.
            limit: Maximum events to return (default 200).

        Returns a list of :class:`DiagnosticEvent` rows in newest-first
        order.
        """
        board = self._board_for(ticket_id) if ticket_id else self.board_id
        try:
            with db.session(self.settings, board) as s:
                stmt = select(DiagnosticEvent)
                if category is not None:
                    stmt = stmt.where(DiagnosticEvent.category == category)
                if ticket_id is not None:
                    stmt = stmt.where(DiagnosticEvent.ticket_id == ticket_id)
                if repo_id is not None:
                    stmt = stmt.where(DiagnosticEvent.repo_id == repo_id)
                if sub_category is not None:
                    stmt = stmt.where(DiagnosticEvent.sub_category == sub_category)
                stmt = stmt.order_by(
                    DiagnosticEvent.created_at.desc()  # type: ignore[attr-defined]
                ).limit(limit)
                return list(s.exec(stmt).all())
        except ValueError:
            # _board_for raised — ticket not found on any board
            return []

    def check_recurring_categories(
        self,
        category: str,
        threshold: int,
        *,
        repo_id: str = "",
    ) -> list[dict[str, Any]]:
        """Aggregate *category* events by sub-category and return groups
        whose distinct-ticket count meets or exceeds *threshold*.

        Each returned dict has keys ``category``, ``sub_category``,
        ``distinct_tickets`` (count), ``reason`` (first event's reason),
        ``ticket_ids`` (up to 10 sample ids).

        Returns an empty list when no group crosses the threshold.
        """
        board = repo_id or self.board_id
        try:
            with db.session(self.settings, board) as s:
                # Count distinct ticket_ids per (category, sub_category).
                rows = s.exec(
                    select(
                        DiagnosticEvent.category,
                        DiagnosticEvent.sub_category,
                        func.count(func.distinct(DiagnosticEvent.ticket_id)).label(
                            "cnt"
                        ),
                    )
                    .where(DiagnosticEvent.category == category)
                    .group_by(DiagnosticEvent.category, DiagnosticEvent.sub_category)
                    .having(
                        func.count(func.distinct(DiagnosticEvent.ticket_id))
                        >= threshold
                    )
                ).all()

                results: list[dict[str, Any]] = []
                for cat, sub_cat, cnt in rows:
                    # Grab a sample reason and a few ticket ids for context.
                    sample = s.exec(
                        select(DiagnosticEvent)
                        .where(
                            DiagnosticEvent.category == cat,
                            DiagnosticEvent.sub_category == sub_cat,
                        )
                        .order_by(
                            DiagnosticEvent.created_at.desc()  # type: ignore[attr-defined]
                        )
                        .limit(10)
                    ).all()
                    results.append(
                        {
                            "category": cat,
                            "sub_category": sub_cat,
                            "distinct_tickets": cnt,
                            "reason": sample[0].reason if sample else "",
                            "ticket_ids": [e.ticket_id for e in sample],
                        }
                    )
                return results
        except ValueError:
            return []

    def count_distinct_tickets_for_category(
        self,
        category: str,
        sub_category: str,
        *,
        repo_id: str = "",
    ) -> int:
        """Return the number of distinct ticket_ids for a given category
        and sub-category pair.

        Used by tests and the diagnostic check to verify against the
        configured threshold.
        """
        board = repo_id or self.board_id
        try:
            with db.session(self.settings, board) as s:
                result = s.exec(
                    select(func.count(func.distinct(DiagnosticEvent.ticket_id))).where(
                        DiagnosticEvent.category == category,
                        DiagnosticEvent.sub_category == sub_category,
                    )
                ).one()
                return int(result)
        except ValueError:
            return 0
