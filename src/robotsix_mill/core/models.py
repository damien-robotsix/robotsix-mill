"""Management-plane data model (SQLModel / SQLite).

The DB is authoritative for *management* — state, history, queueing,
relationships — and is what the API (and a future web frontend) read.
The ticket *body* is not stored here: it lives in the filesystem
workspace (``description.md``); the row keeps only ``workspace_path`` and
``content_hash`` as the pointer. SQLModel classes double as API schemas.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column
from sqlmodel import Field, SQLModel

from .datetime_utils import TZDateTime
from .states import State


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Ticket(SQLModel, table=True):
    id: str = Field(primary_key=True)
    title: str
    state: State = Field(default=State.DRAFT, index=True)
    # pointer into the work plane (file-canonical body)
    workspace_path: str
    content_hash: str = ""
    # set by the implement stage
    branch: str | None = None
    # epic / sub-ticket relationships (future use)
    parent_id: str | None = Field(default=None, foreign_key="ticket.id")
    # which actor created the ticket — free-form for forward compatibility
    # (e.g. "user", "retrospect", "some-future-agent")
    source: str = Field(default="user")
    # when blocked, which state the ticket was in before being blocked;
    # enables the BLOCKED → <originating state> resume path so only the
    # failed stage is re-run.
    blocked_from: str | None = Field(default=None)
    # Langfuse session id that produced this ticket (set at creation by
    # agent emission sites; None for human/API-created tickets).
    origin_session: str | None = Field(default=None)
    # cumulative LLM spend in USD, synced from Langfuse session totals
    # by the periodic cost-sync loop. Zero when Langfuse is unconfigured.
    cost_usd: float = Field(default=0.0)
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(TZDateTime()),
    )
    updated_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(TZDateTime()),
    )


class TicketEvent(SQLModel, table=True):
    """Append-only state-transition history."""

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: str = Field(foreign_key="ticket.id", index=True)
    state: State
    note: str | None = None
    at: datetime = Field(
        default_factory=_now,
        sa_column=Column(TZDateTime()),
    )


class Comment(SQLModel, table=True):
    """Reviewer comment on a ticket — append-only, single-level."""

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: str = Field(foreign_key="ticket.id", index=True)
    body: str
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(TZDateTime()),
    )


# --- API request/response shapes ---


class TicketCreate(SQLModel):
    title: str
    description: str = ""


class TicketTransition(SQLModel):
    state: State
    note: str | None = None


class TicketRead(SQLModel):
    id: str
    title: str
    state: State
    branch: str | None
    parent_id: str | None
    source: str
    origin_session: str | None
    origin_session_url: str | None
    cost_usd: float
    created_at: datetime
    updated_at: datetime


class CommentCreate(SQLModel):
    body: str


class CommentRead(SQLModel):
    id: int
    ticket_id: str
    body: str
    created_at: datetime
