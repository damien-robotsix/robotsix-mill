"""Ticket-creation surface of :class:`TicketService` (``_CreateMixin``)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from secrets import token_hex

from sqlmodel import Session, select

from .. import db
from ..db import retry_on_db_full
from ..models import (
    Comment,
    SourceKind,
    Ticket,
    TicketEvent,
    TicketKind,
)
from ..states import ASK_USER_MARKER, State
from ..workspace import Workspace
from ._base import _ServiceBase
from ._helpers import (
    _get_ticket,
    _make_event,
    _parse_depends_on_str,
    _slug,
)

log = logging.getLogger("robotsix_mill.service")


class _CreateMixin(_ServiceBase):
    """Ticket creation, step events, and ask-user thread helpers."""

    def create(
        self,
        title: str,
        description: str = "",
        source: str = SourceKind.USER,
        origin_session: str | None = None,
        depends_on: str | None = None,
        unblocks: str | None = None,
        kind: TicketKind = TicketKind.TASK,
        parent_id: str | None = None,
        board_id: str | None = None,
        priority: bool = False,
    ) -> Ticket:
        """Create a new ticket with the given *title*.

        Side effects: creates a :class:`Workspace`, writes the optional
        *description* file, persists the :class:`Ticket` and a
        ``"created"`` :class:`TicketEvent`.

        The ticket id is constructed from the UTC timestamp, a slug of
        the title, and a short random hex suffix.

        When *kind* is ``"inquiry"`` the initial state is ``ASKED``
        (the answer stage picks it up) instead of ``DRAFT``.
        When *kind* is ``"epic"`` the initial state is ``EPIC_OPEN``.
        ``depends_on`` is NOT allowed for inquiries or epics — raises
        :class:`ValueError`.

        If *parent_id* is provided, the parent ticket must exist; the
        created ticket is linked to it via ``set_parent``.

        *board_id* overrides ``self.board_id`` when provided — used by
        the multi-repo API surface to stamp the correct board on each
        ticket.

        Raises :class:`ValueError` if *depends_on* includes the ticket's
        own ID (self-dependency), is provided for an inquiry or epic, or
        if *parent_id* references a nonexistent ticket.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ticket_id = f"{stamp}-{_slug(title)}-{token_hex(2)}"

        if kind in (TicketKind.INQUIRY, TicketKind.EPIC) and depends_on:
            raise ValueError(f"{kind}s do not support depends_on — they are standalone")

        # Reject self-dependency before persisting.
        if depends_on:
            dep_ids = _parse_depends_on_str(depends_on)
            if ticket_id in dep_ids:
                raise ValueError(f"Ticket cannot depend on itself: {ticket_id}")

        if kind == TicketKind.EPIC:
            initial_state = State.EPIC_OPEN
        elif kind == TicketKind.INQUIRY:
            initial_state = State.ASKED
        else:
            initial_state = State.DRAFT

        # Route to the right per-repo DB / workspace: use the
        # explicit board_id override when provided (the route
        # creates a ticket for a different repo than this service
        # is bound to), else self.board_id.
        effective_board = board_id if board_id is not None else self.board_id

        # In multi-repo mode every ticket MUST belong to a board —
        # otherwise it ends up in the default mill.db and the UI
        # can't find it (the per-repo list endpoints filter by
        # board_id). Reject board-less creates so an agent tool
        # that forgot to thread board_id raises here instead of
        # silently producing an orphan ticket + an orphan
        # ``.data/workspaces/<id>`` directory.
        if not effective_board:
            from ...config import get_repos_config

            try:
                repos = get_repos_config().repos
            except Exception:
                repos = {}
            if repos and not self.settings.default_repo_id:
                raise ValueError(
                    "refusing to create board-less ticket in multi-repo "
                    "mode: pass an explicit board_id, or configure "
                    "MILL_DEFAULT_REPO_ID. "
                    f"(title={title!r}, source={source!r})"
                )

        # Validate parent_id against ANY board (cross-board parent links are
        # supported — the epic may live on a different board than its child).
        if parent_id is not None:
            parent = self.get(parent_id)
            if parent is None:
                raise ValueError(f"parent_id {parent_id!r} does not exist")

        ws = Workspace(self.settings.workspaces_dir_for(effective_board), ticket_id)
        content_hash = ws.write_description(description)
        # Inherit priority from any priority-marked ancestor at
        # creation time. set_priority on an epic propagates to
        # CURRENT children; this walk catches children created AFTER
        # the epic was flagged. Loop is bounded by parent-chain depth
        # and skips cycles (which shouldn't exist but cheap to guard).
        inherited_priority = False
        if parent_id is not None:
            seen: set[str] = set()
            cur = parent_id
            while cur and cur not in seen:
                seen.add(cur)
                with db.session(self.settings, effective_board) as s:
                    p = s.get(Ticket, cur)
                if p is None:
                    break
                if getattr(p, "priority", False):
                    inherited_priority = True
                    break
                cur = p.parent_id
        with retry_on_db_full(self.settings, effective_board) as s:
            ticket = Ticket(
                id=ticket_id,
                title=title,
                state=initial_state,
                kind=kind,
                workspace_path=str(ws.dir),
                content_hash=content_hash,
                source=source,
                origin_session=origin_session,
                depends_on=depends_on,
                unblocks=unblocks,
                parent_id=parent_id,
                board_id=board_id if board_id is not None else self.board_id,
                priority=priority or inherited_priority,
            )
            s.add(ticket)
            s.flush()
            s.add(
                _make_event(s, ticket_id=ticket_id, state=initial_state, note="created")
            )
            s.commit()
            s.refresh(ticket)
            return ticket

    def _has_active_child(self, ticket_id: str) -> bool:
        """Return True if *ticket_id* has at least one child whose
        state is NOT in ``_ARCHIVABLE_STATES``."""
        with db.session(self.settings, self.board_id) as s:
            stmt = (
                select(Ticket)
                .where(
                    Ticket.parent_id == ticket_id,
                    Ticket.state.notin_(list(self._ARCHIVABLE_STATES)),
                )
                .limit(1)
            )
            return s.exec(stmt).first() is not None

    def add_step_event(
        self,
        ticket_id: str,
        note: str,
    ) -> None:
        """Append a same-state event to a ticket's history.

        For agent conclusions that don't change state — scope-triage
        EXPAND continues the implement loop, doc-classifier verdict
        leaves the stage running. Those used to be emitted as
        comments so the UI showed them; they now live in history so
        comments stay reserved for human/agent interaction (ASK_USER,
        code review threads).

        The event carries the ticket's CURRENT state and the
        ``note`` describing what the agent concluded. The hash chain
        is extended like any other event.
        """
        with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            s.add(
                _make_event(
                    s,
                    ticket_id=ticket_id,
                    state=ticket.state,
                    note=note,
                )
            )
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()

    def _has_open_ask_user_threads(
        self, ticket_id: str, session: Session
    ) -> list[Comment]:
        """Return open top-level ``[ASK_USER]`` comment threads on
        *ticket_id* (those with ``closed_at IS NULL``)."""
        stmt = select(Comment).where(
            Comment.ticket_id == ticket_id,
            Comment.parent_id.is_(None),
            Comment.body.startswith(ASK_USER_MARKER),
            Comment.closed_at.is_(None),
        )
        return list(session.exec(stmt).all())

    def close_open_ask_user_threads(self, ticket_id: str) -> int:
        """Close every open ``[ASK_USER]`` thread on *ticket_id*; return the
        count closed.

        Used when the pipeline AUTO-completes a ticket (e.g. a merged PR
        reaching DONE) whose open questions are now moot — the work shipped,
        so a stale thread must not block the terminal transition (which would
        otherwise raise ``TransitionError`` and crash the worker consumer in a
        loop). The thread is closed-with-record (not deleted), so the question
        text is preserved in history.
        """
        with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
            open_threads = self._has_open_ask_user_threads(ticket_id, s)
            now = datetime.now(timezone.utc)
            for c in open_threads:
                c.closed_at = now
                s.add(c)
            s.commit()
            return len(open_threads)

    def add_history_note(self, ticket_id: str, note: str) -> TicketEvent:
        """Append a non-transition history entry that records an
        informational note on the ticket.

        Used for the post-stage Langfuse trace breadcrumb. Previously
        the worker posted that link as a comment (author=mill); refine
        and implement then read the comment stream and treated the
        inaccessible URL as reviewer feedback. Writing to history
        instead keeps the audit trail visible to a human browsing the
        ticket without contaminating the channel agents read.

        The event reuses the ticket's CURRENT state — it's a side-band
        note, not a transition. Hash chain stays intact: the next real
        transition's ``prev_hash`` correctly points at this entry.
        """
        with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
            ticket = _get_ticket(s, ticket_id)
            event = _make_event(s, ticket_id=ticket_id, state=ticket.state, note=note)
            s.add(event)
            s.commit()
            s.refresh(event)
            return event
