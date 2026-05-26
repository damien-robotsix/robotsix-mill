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

from enum import StrEnum

from .datetime_utils import TZDateTime
from .states import State


class SourceKind(StrEnum):
    USER = "user"
    RETROSPECT = "retrospect"
    AUDIT = "audit"
    SURVEY = "survey"
    AGENT = "agent"
    CI = "ci"
    HEALTH = "health"
    ENV_SYNC = "env_sync"
    TEST_GAP = "test_gap"
    AGENT_CHECK = "agent_check"
    BC_CHECK = "bc_check"
    COST_RECONCILIATION = "cost_reconciliation"
    COMPLETENESS_CHECK = "completeness_check"
    TRACE_HEALTH = "trace-health"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Ticket(SQLModel, table=True):
    id: str = Field(primary_key=True)
    title: str
    state: State = Field(default=State.DRAFT, index=True)
    kind: str = Field(default="task")  # "task", "inquiry", or "epic"
    # pointer into the work plane (file-canonical body)
    workspace_path: str
    content_hash: str = ""
    # set by the implement stage
    branch: str | None = None
    # epic / sub-ticket relationships (future use)
    parent_id: str | None = Field(default=None, foreign_key="ticket.id")
    # which actor created the ticket — free-form for forward compatibility
    # (e.g. "user", "retrospect", "some-future-agent")
    source: str = Field(default=SourceKind.USER)
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
    # Count of consecutive REQUEST_CHANGES verdicts inside the current
    # review session.  Reset on APPROVE or when the cap is hit; persists
    # across the CODE_REVIEW → READY → DOCUMENTING → CODE_REVIEW loop.
    review_rounds: int = Field(default=0)
    # transient-error retry state (stage-runner level, not LLM-call level)
    retry_attempt: int = Field(default=0)
    last_transient_error: str | None = Field(default=None)
    next_retry_at: datetime | None = Field(default=None, sa_column=Column(TZDateTime(), nullable=True))
    # optional JSON list of ticket IDs that must reach CLOSED/DONE before
    # this ticket can leave READY (implement-stage gate).
    depends_on: str | None = Field(default=None)
    # board_id from RepoConfig — stamped at creation so every ticket
    # is tagged with its repository.  Empty string for legacy rows.
    board_id: str = Field(default="", index=True)
    # Operator-controlled priority: when True the worker pulls this
    # ticket off the queue ahead of non-priority ones (used to jump
    # bug-fix tickets in front of the normal backlog).
    priority: bool = Field(default=False, index=True)
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(TZDateTime(), index=True),
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
    """Reviewer comment on a ticket — supports threading via parent_id
    and open/closed tracking on top-level threads via closed_at."""

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: str = Field(foreign_key="ticket.id", index=True)
    body: str
    author: str = Field(default="user")
    parent_id: int | None = Field(default=None, foreign_key="comment.id", nullable=True)
    closed_at: datetime | None = Field(default=None, sa_column=Column(TZDateTime(), nullable=True))
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(TZDateTime()),
    )


# --- API request/response shapes ---


class TicketCreate(SQLModel):
    title: str
    description: str = ""
    depends_on: str | None = None
    source: str = SourceKind.USER
    kind: str = "task"  # "task", "inquiry", or "epic"
    parent_id: str | None = None
    repo_id: str | None = None


class TicketTransition(SQLModel):
    state: State
    note: str | None = None


class TicketRead(SQLModel):
    id: str
    title: str
    state: State
    kind: str
    branch: str | None
    parent_id: str | None
    parent_title: str | None = None
    source: str
    origin_session: str | None
    origin_session_url: str | None
    cost_usd: float
    cumulative_cost: float | None = None
    depends_on: str | None
    unmet_deps: list[str]
    pr_url: str | None = None
    retry_attempt: int
    last_transient_error: str | None
    next_retry_at: datetime | None
    priority: bool = False
    created_at: datetime
    updated_at: datetime


class CommentCreate(SQLModel):
    body: str
    author: str = "user"
    parent_id: int | None = None


class CommentRead(SQLModel):
    id: int
    ticket_id: str
    body: str
    author: str
    parent_id: int | None
    closed_at: datetime | None
    created_at: datetime
