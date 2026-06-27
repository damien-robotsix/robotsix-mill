"""Ticket metadata mutators for ``TicketService`` (the ``_MetadataMixin``).

These setters write a single column (or a small, related group) on a ticket
row without driving a state transition: relationships (``set_parent``,
``set_unblocks``, ``set_depends_on``), display/queue metadata
(``set_labels``, ``set_priority``, ``set_title``, ``set_branch``,
``set_review_rounds``), the file-pointer hash (``set_content_hash``), and
the ``promote_to_epic`` kind-flip. They are split out of
:mod:`._lifecycle` purely to keep each submodule within the package's
per-file line ceiling; the assembled :class:`TicketService` exposes them
exactly as before via the mixin MRO.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .. import db
from ..models import Ticket, TicketKind
from ._base import _ServiceBase
from ._helpers import _get_ticket


class _MetadataMixin(_ServiceBase):
    """Single-column ticket metadata setters (no state transition)."""

    def set_unblocks(self, ticket_id: str, target_ids: list[str]) -> Ticket:
        """Set the list of ticket IDs *ticket_id* auto-unblocks on completion.

        Stored as a JSON array; replaces any prior value. Self-references are
        dropped. Returns the updated ticket; raises ``KeyError`` if unknown.
        """
        cleaned = [t for t in dict.fromkeys(target_ids) if t and t != ticket_id]
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.unblocks = json.dumps(cleaned) if cleaned else None
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()
            s.refresh(ticket)
            return ticket

    def set_labels(self, ticket_id: str, labels: list[str]) -> Ticket:
        """Set the free-form label list applied to *ticket_id*.

        Stored as a JSON array; replaces any prior value. Duplicates are
        dropped preserving order; an empty list is stored as ``None``.
        Returns the updated ticket; raises ``KeyError`` if unknown.
        """
        cleaned: list[str] = list(dict.fromkeys(labels))
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.labels = json.dumps(cleaned) if cleaned else None
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()
            s.refresh(ticket)
            return ticket

    def set_priority(self, ticket_id: str, priority: bool) -> list[str]:
        """Toggle the operator-controlled priority flag on a ticket.

        When True, the worker pulls this ticket off the queue ahead of
        non-priority tickets — used to jump bug-fix tickets in front of
        the normal backlog without changing dependency wiring.

        Epic propagation: when the target ticket has descendants (epic
        with children, sub-epics, etc.) the flag is applied to every
        descendant too. Children created LATER also inherit the
        priority via the create-time parent-chain walk (see
        :meth:`create`). Returns the list of ticket IDs whose priority
        was changed (the target plus any affected descendants) so the
        caller can re-enqueue each one.
        """
        changed: list[str] = []
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            new_value = bool(priority)
            if ticket.priority != new_value:
                ticket.priority = new_value
                ticket.updated_at = datetime.now(timezone.utc)
                s.add(ticket)
                changed.append(ticket.id)
                s.commit()
                if self._on_transition is not None:
                    self._on_transition(ticket)
            else:
                s.commit()
        # Propagate to every descendant. _all_descendants walks the
        # parent_id graph and is cycle-safe.
        for descendant in self._all_descendants(ticket_id):
            with db.session(self.settings, self._board_for(descendant.id)) as s:
                d = s.get(Ticket, descendant.id)
                if d is None or d.priority == bool(priority):
                    continue
                d.priority = bool(priority)
                d.updated_at = datetime.now(timezone.utc)
                s.add(d)
                s.commit()
                changed.append(d.id)
                if self._on_transition is not None:
                    self._on_transition(d)
        return changed

    def set_branch(self, ticket_id: str, branch: str) -> None:
        """Record the git branch name for a ticket.

        Raises :class:`KeyError` if the ticket does not exist.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.branch = branch
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_parent(self, ticket_id: str, parent_id: str) -> None:
        """Link a spawned ticket to the ticket it originated from
        (e.g. a retrospect improvement draft -> the reviewed ticket)."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.parent_id = parent_id
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_title(self, ticket_id: str, title: str) -> None:
        """Update the title of a ticket. Raises :class:`KeyError` if
        the ticket does not exist."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.title = title
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_content_hash(self, ticket_id: str, content_hash: str) -> None:
        """Keep the DB pointer in sync after a stage rewrites the
        file-canonical description (so it isn't seen as an external edit)."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.content_hash = content_hash
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def promote_to_epic(self, ticket_id: str) -> None:
        """Flip a task ticket's kind to ``epic`` without changing state.

        Used by the refine stage's ``promote_to_epic`` path: refine flips
        the kind here, then the stage returns ``Outcome(EPIC_OPEN, …)``
        and the worker performs the actual state transition through the
        standard ``transition()`` path (which writes the state event).

        No-op for tickets already kind=epic. Raises ``KeyError`` for
        unknown ids.
        """
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            if ticket.kind == TicketKind.EPIC:
                return
            ticket.kind = TicketKind.EPIC
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_review_rounds(self, ticket_id: str, value: int) -> None:
        """Set the ``review_rounds`` counter on *ticket_id*."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.review_rounds = value
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_implement_cycles(self, ticket_id: str, value: int) -> None:
        """Set the ``implement_cycles`` counter on *ticket_id*.

        Tracks total implement passes across all review rounds
        (ticket lifetime).  Used by the implement↔review convergence
        backstop."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.implement_cycles = value
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def set_depends_on(self, ticket_id: str, depends_on_ids: list[str]) -> None:
        """Set the ``depends_on`` field for *ticket_id* to a JSON-encoded
        list of ticket IDs.  Raises :class:`ValueError` if *ticket_id*
        appears in *depends_on_ids* (self-dependency)."""
        if ticket_id in depends_on_ids:
            raise ValueError(f"Ticket cannot depend on itself: {ticket_id}")
        raw = json.dumps(depends_on_ids) if depends_on_ids else None
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            ticket.depends_on = raw
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()
