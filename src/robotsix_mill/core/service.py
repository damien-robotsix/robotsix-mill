"""TicketService — the management-plane API surface over the DB.

All state mutation goes through here so the API, the worker, and tests
share one set of invariants (transition validation, history events,
workspace pointer upkeep). DB access is synchronous; the worker calls it
from its coroutine (never from the stage threadpool).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from secrets import token_hex

from sqlmodel import select

from . import db
from ..config import Settings
from .models import Ticket, TicketEvent
from .states import State, can_transition
from .workspace import Workspace

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:40] or "ticket"


class TransitionError(RuntimeError):
    """Requested state transition is not allowed by the state machine."""


class TicketService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def workspace(self, ticket: Ticket) -> Workspace:
        return Workspace(self.settings.workspaces_dir, ticket.id)

    # --- reads ---
    def get(self, ticket_id: str) -> Ticket | None:
        with db.session(self.settings) as s:
            return s.get(Ticket, ticket_id)

    def list(self, state: State | None = None) -> list[Ticket]:
        with db.session(self.settings) as s:
            stmt = select(Ticket).order_by(Ticket.created_at)
            if state is not None:
                stmt = stmt.where(Ticket.state == state)
            return list(s.exec(stmt).all())

    def history(self, ticket_id: str) -> list[TicketEvent]:
        with db.session(self.settings) as s:
            stmt = (
                select(TicketEvent)
                .where(TicketEvent.ticket_id == ticket_id)
                .order_by(TicketEvent.at)
            )
            return list(s.exec(stmt).all())

    # --- writes ---
    def create(self, title: str, description: str = "") -> Ticket:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ticket_id = f"{stamp}-{_slug(title)}-{token_hex(2)}"
        ws = Workspace(self.settings.workspaces_dir, ticket_id)
        content_hash = ws.write_description(description)
        with db.session(self.settings) as s:
            ticket = Ticket(
                id=ticket_id,
                title=title,
                state=State.DRAFT,
                workspace_path=str(ws.dir),
                content_hash=content_hash,
            )
            s.add(ticket)
            s.add(
                TicketEvent(
                    ticket_id=ticket_id, state=State.DRAFT, note="created"
                )
            )
            s.commit()
            s.refresh(ticket)
            return ticket

    def transition(
        self, ticket_id: str, dst: State, note: str | None = None
    ) -> Ticket:
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            if not can_transition(ticket.state, dst):
                raise TransitionError(
                    f"{ticket_id}: {ticket.state} -> {dst} not allowed"
                )
            ticket.state = dst
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.add(TicketEvent(ticket_id=ticket_id, state=dst, note=note))
            s.commit()
            s.refresh(ticket)
            return ticket

    def set_branch(self, ticket_id: str, branch: str) -> None:
        with db.session(self.settings) as s:
            ticket = s.get(Ticket, ticket_id)
            if ticket is None:
                raise KeyError(ticket_id)
            ticket.branch = branch
            ticket.updated_at = datetime.now(timezone.utc)
            s.add(ticket)
            s.commit()
