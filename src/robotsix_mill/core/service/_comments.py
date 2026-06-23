"""Comment / thread surface of :class:`TicketService` (``_CommentMixin``)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlmodel import select

from .. import db
from ..models import Comment, Ticket
from ..states import ASK_USER_MARKER, State
from ._base import _ServiceBase
from ._helpers import _get_ticket, _make_event

log = logging.getLogger("robotsix_mill.service")


class _CommentMixin(_ServiceBase):
    """Reviewer comments, thread close/reopen, and ask-user auto-resume."""

    def add_comment(
        self,
        ticket_id: str,
        body: str,
        author: str = "user",
        parent_id: int | None = None,
    ) -> Comment:
        """Add a reviewer comment to a ticket. Raises ``KeyError`` if
        the ticket does not exist.

        When *parent_id* is given, validates that the parent Comment
        exists and belongs to the same ticket, raising ``ValueError``
        otherwise."""
        with db.session(self.settings, self._board_for(ticket_id)) as s:
            _get_ticket(s, ticket_id)
            if parent_id is not None:
                parent = s.get(Comment, parent_id)
                if parent is None:
                    raise ValueError(f"parent comment {parent_id} not found")
                if parent.ticket_id != ticket_id:
                    # List valid thread IDs for this ticket so the error
                    # is self-diagnosing — callers can discover the
                    # correct IDs without a separate round-trip.
                    valid_stmt = (
                        select(Comment)
                        .where(Comment.ticket_id == ticket_id)
                        .where(Comment.parent_id.is_(None))
                    )
                    valid_threads = [c.id for c in s.exec(valid_stmt).all()]
                    raise ValueError(
                        f"parent comment {parent_id} does not belong to ticket {ticket_id}. "
                        f"Valid thread IDs for this ticket: {valid_threads}"
                    )
            comment = Comment(
                ticket_id=ticket_id, body=body, author=author, parent_id=parent_id
            )
            s.add(comment)
            s.commit()
            s.refresh(comment)
            return comment

    def _board_for_comment(
        self,
        comment_id: int,
        ticket_id: str | None = None,
    ) -> str:
        """Resolve the board that owns *comment_id*.

        ``Comment.id`` is per-board auto-increment (each repo's
        SQLite assigns its own integer sequence), so a bare comment
        id is ambiguous across boards. When *ticket_id* is provided
        the lookup is unambiguous — the comment lives on the same
        board as its ticket. The route handlers always have the
        ticket id in hand (the user is on a ticket page when closing
        a thread), so this is the production path.

        Fall back to a cross-board fanout when *ticket_id* is missing,
        purely for backward compatibility with callers that haven't
        been threaded through yet. The fanout picks the first board
        whose DB contains a matching id — wrong on collisions, but
        no worse than the prior behaviour.

        Raises ``ValueError`` when no matching comment is found and
        ``self.board_id`` is empty.
        """
        if ticket_id is not None:
            return self._board_for(ticket_id)

        from ...config import get_repos_config

        candidates: list[str] = [self.board_id] if self.board_id else []

        try:
            for rc in get_repos_config().repos.values():
                if rc.board_id and rc.board_id not in candidates:
                    candidates.append(rc.board_id)
        except Exception as exc:
            log.warning(
                "Failed to load repos config for _board_for_comment: %s(%r)",
                type(exc).__name__,
                exc,
            )
        try:
            for sub in self.settings.data_dir.iterdir():
                if sub.is_dir() and (sub / "mill.db").exists():
                    if sub.name not in candidates:
                        candidates.append(sub.name)
        except OSError:
            pass
        for board_id in candidates:
            with db.session(self.settings, board_id) as s:
                if s.get(Comment, comment_id) is not None:
                    return board_id
        if self.board_id:
            return self.board_id
        raise ValueError(
            f"Comment {comment_id} not found in any configured board "
            f"(searched: {candidates or '<none>'})"
        )

    def close_thread(
        self,
        comment_id: int,
        ticket_id: str | None = None,
    ) -> Comment:
        """Close a top-level comment thread.  Raises ``KeyError`` if
        the comment does not exist, ``ValueError`` if it is a reply
        (non-NULL parent_id) or is already closed.

        When the closed thread was an ``[ASK_USER]`` question on a
        ticket in ``AWAITING_USER_REPLY``, and every other
        ``[ASK_USER]`` thread on that ticket is also closed, the ticket
        is automatically resumed to its pre-pause state.

        *ticket_id* disambiguates the board in multi-repo mode (
        ``Comment.id`` is per-board, not globally unique). When the
        caller has the ticket id in hand (e.g. from the UI / agent
        tool) it MUST be passed — without it the lookup falls back
        to a cross-board fanout that picks the first board whose
        SQLite happens to have a matching id, which is the wrong
        comment on a collision.
        """
        board = self._board_for_comment(comment_id, ticket_id)
        with db.session(self.settings, board) as s:
            comment = s.get(Comment, comment_id)
            if comment is None:
                raise KeyError(f"comment {comment_id} not found")
            if comment.parent_id is not None:
                raise ValueError("only top-level threads can be closed")
            if comment.closed_at is not None:
                raise ValueError("thread already closed")
            comment.closed_at = datetime.now(timezone.utc)
            s.add(comment)
            ticket_id = comment.ticket_id
            s.commit()
            s.refresh(comment)

        # Post-close: auto-resume if all [ASK_USER] threads on a paused
        # ticket are now closed.  Use the SAME board (and a fresh
        # session) so the commit above is visible.
        self._maybe_resume_awaiting_user_reply(ticket_id, board)

        return comment

    def _maybe_resume_awaiting_user_reply(
        self,
        ticket_id: str,
        board: str,
    ) -> None:
        """If *ticket_id* is in ``AWAITING_USER_REPLY`` and every
        top-level ``[ASK_USER]`` comment thread on it is closed,
        transition the ticket back to its ``paused_from`` state."""
        with db.session(self.settings, board) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None or ticket.state is not State.AWAITING_USER_REPLY:
                return

            if not ticket.paused_from:
                log.warning(
                    "%s: AWAITING_USER_REPLY but no paused_from — cannot auto-resume",
                    ticket_id,
                )
                return

            # Count all top-level [ASK_USER] threads and check whether
            # every one is closed.
            stmt = select(Comment).where(
                Comment.ticket_id == ticket_id,
                Comment.parent_id == None,  # noqa: E711 (SQLAlchemy needs == None for SQL IS NULL)
                Comment.body.startswith(ASK_USER_MARKER),
            )
            ask_threads = list(s.exec(stmt).all())

            # No [ASK_USER] threads at all → skip (shouldn't happen on a
            # legitimately paused ticket, but be defensive).
            if not ask_threads:
                return

            if any(t.closed_at is None for t in ask_threads):
                return  # at least one still open

            # All [ASK_USER] threads closed → resume.
            dst = State(ticket.paused_from)
            ticket.blocked_from = None
            ticket.paused_from = None
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.flush()
            s.add(
                _make_event(
                    s,
                    ticket_id=ticket_id,
                    state=dst,
                    note="all ask_user threads closed — resuming",
                )
            )
            s.commit()
            s.refresh(ticket)
            log.info(
                "%s: auto-resumed from AWAITING_USER_REPLY → %s "
                "(all %d ask_user threads closed)",
                ticket_id,
                dst.value,
                len(ask_threads),
            )
            if self._on_transition is not None:
                self._on_transition(ticket)

    def reopen_thread(
        self,
        comment_id: int,
        ticket_id: str | None = None,
    ) -> Comment:
        """Reopen a closed top-level comment thread.  Raises
        ``KeyError`` if the comment does not exist, ``ValueError`` if
        it is a reply (non-NULL parent_id) or is not currently closed."""
        with db.session(
            self.settings, self._board_for_comment(comment_id, ticket_id)
        ) as s:
            comment = s.get(Comment, comment_id)
            if comment is None:
                raise KeyError(f"comment {comment_id} not found")
            if comment.parent_id is not None:
                raise ValueError("only top-level threads can be reopened")
            if comment.closed_at is None:
                raise ValueError("thread is not closed")
            comment.closed_at = None
            s.add(comment)
            s.commit()
            s.refresh(comment)
            return comment
