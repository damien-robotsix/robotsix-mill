"""Management-plane data model (SQLModel / SQLite).

The DB is authoritative for *management* — state, history, queueing,
relationships — and is what the API (and a future web frontend) read.
The ticket *body* is not stored here: it lives in the filesystem
workspace (``description.md``); the row keeps only ``workspace_path`` and
``content_hash`` as the pointer. SQLModel classes double as API schemas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Column, TypeDecorator
from sqlalchemy import String as SAString
from sqlmodel import Field, SQLModel

from .datetime_utils import TZDateTime
from .states import State


class SourceKind(StrEnum):
    """Enumeration of ticket origin sources (user, retrospect, audit, etc.)."""

    USER = "user"
    RETROSPECT = "retrospect"
    AUDIT = "audit"
    SURVEY = "survey"
    AGENT = "agent"
    CI = "ci"
    HEALTH = "health"
    CONFIG_SYNC = "config_sync"
    MEMBER_SYNC = "member_sync"
    TEST_GAP = "test_gap"
    AGENT_CHECK = "agent_check"
    BC_CHECK = "bc_check"
    DATA_DIR_GC = "data_dir_gc"
    DEPENDABOT_ALERTS = "dependabot_alerts"
    COMPLETENESS_CHECK = "completeness_check"
    COPY_PASTE = "copy_paste"
    FORGE_PARITY = "forge_parity"
    TRACE_HEALTH = "trace-health"
    TRACE_REVIEW = "trace-review"
    MODULE_CURATOR = "module_curator"
    ROADMAP_SYNC = "roadmap_sync"
    STATE_SYNC = "state_sync"
    FRONTEND_SYNC = "frontend_sync"
    TRIAGE_BOILERPLATE = "triage_boilerplate"
    DOCSTRING_COVERAGE = "docstring_coverage"
    MODULE_SIZE = "module_size"
    MYPY_BASELINE = "mypy_baseline"
    META = "meta"
    RUN_HEALTH = "run-health"
    LANGFUSE_CLEANUP = "langfuse-cleanup"
    CI_FIX_DEPENDENCY = "ci_fix_dependency"
    IMPLEMENT_BASELINE_DEPENDENCY = "implement_baseline_dependency"
    ORPHANED_PR_CHECK = "orphaned_pr_check"
    PERIODIC_CONFIG = "periodic_config"
    REPO_DESCRIPTION_SYNC = "repo_description_sync"
    CONFIG_STANDARD = "config_standard"
    INFRASTRUCTURE = "infrastructure"


class TicketKind(StrEnum):
    """Enumeration of ticket kinds — canonical source of truth for ``kind`` values.

    Persisted as the canonical UPPERCASE member *name* via
    ``CaseTolerantEnum`` (below).  ``State`` is the other name-mapped
    StrEnum and is intentionally left with its auto-generated ``Enum``
    column — not in scope for this ticket.
    """

    TASK = "task"
    INQUIRY = "inquiry"
    EPIC = "epic"


class CaseTolerantEnum(TypeDecorator[str]):
    """Stores a StrEnum by its canonical UPPERCASE member name and
    tolerates any-case DB strings on read (legacy lowercase rows resolve
    cleanly).

    WRITE
        Accept a ``TicketKind`` member or a ``str`` of any case →
        store the canonical uppercase member name.
    READ
        Upper-case the DB string before the enum NAME lookup so a
        legacy lowercase ``'task'`` resolves to ``TicketKind.TASK``.
    """

    impl = SAString
    cache_ok = True

    def __init__(self, enum_cls: type[StrEnum], **kw: object) -> None:
        self.enum_cls = enum_cls
        super().__init__(**kw)

    def process_bind_param(
        self, value: StrEnum | str | None, dialect: object
    ) -> str | None:
        """Convert *value* to its canonical uppercase member name for DB storage.

        Accepts ``None`` (pass-through), an existing ``StrEnum`` member (stores
        its ``.name``), or a ``str`` of any case (resolved to the canonical
        member name via uppercase lookup).  Raises ``ValueError`` when the
        string does not match any member of the enum class.
        """
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return value.name  # canonical uppercase, e.g. 'TASK'
        # Accept any-case str and resolve to canonical member name.
        try:
            return self.enum_cls[str(value).upper()].name
        except KeyError as exc:
            raise ValueError(
                f"{str(value)!r} is not a valid {self.enum_cls.__name__} value"
            ) from exc

    def process_result_value(
        self, value: str | None, dialect: object
    ) -> StrEnum | None:
        """Restore a DB string to the corresponding ``StrEnum`` member.

        Accepts ``None`` (pass-through) or a ``str`` — uppercased and resolved
        against ``self.enum_cls``.  Raises ``ValueError`` when the string does
        not match any member name.
        """
        if value is None:
            return None
        try:
            return self.enum_cls[str(value).upper()]
        except KeyError as exc:
            raise ValueError(
                f"{str(value)!r} is not a valid {self.enum_cls.__name__} member name"
            ) from exc


def _now() -> datetime:
    return datetime.now(UTC)


class Ticket(SQLModel, table=True):
    """Database row for a ticket — tracks state, branch, cost, retries, and parent/child relationships."""

    id: str = Field(primary_key=True)
    title: str
    state: State = Field(default=State.DRAFT, index=True)
    kind: TicketKind = Field(
        default=TicketKind.TASK,
        sa_column=Column(CaseTolerantEnum(TicketKind), nullable=False),
    )
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
    # machine-checkable reason for the CURRENT BLOCKED state — a JSON
    # object {"kind": <kind>, ...params}, e.g.
    # {"kind": "target_branch_red", "workflows": ["CI", "Security Audit"]}
    # or {"kind": "unmet_dependency", "ticket_id": "..."}.  Set when
    # transitioning INTO BLOCKED from a structured block site, cleared
    # when leaving BLOCKED.  The periodic block-rechecker reads ONLY this
    # field (never prose) so a reason can be evaluated against live state
    # without substring-matching block notes — a known false-positive and
    # false-negative source.
    block_reason: str | None = Field(default=None)
    # when paused mid-stage (awaiting user reply), which state the ticket
    # was in before the pause; enables the AWAITING_USER_REPLY →
    # <paused_from> resume path so the stage can resume from the question.
    paused_from: str | None = Field(default=None)
    # Langfuse session id that produced this ticket (set at creation by
    # agent emission sites; None for human/API-created tickets).
    origin_session: str | None = Field(default=None)
    # cumulative LLM spend in USD, synced from Langfuse session totals
    # by the periodic cost-sync loop. Zero when Langfuse is unconfigured.
    cost_usd: float = Field(default=0.0)
    # snapshot of the full Langfuse session cost captured at the last
    # redraft. Subtracted from the live session total to yield the
    # effective per-attempt cost used for the dollar-cap limit, so a
    # redraft restarts the cost limit at zero while the full total
    # (including pre-redraft spend) stays available for display.
    pre_redraft_cost_usd: float = Field(default=0.0)
    # snapshot of the full Langfuse session trace count captured at the
    # last operator resume of a trace-circuit-breaker block.  Subtracted
    # from the live session trace count to yield the effective trace
    # count used for the per-ticket trace-count limit, so a resume
    # restarts the trace limit at zero while all traces stay in
    # Langfuse.  A sentinel of -1 means "set baseline on next poll."
    pre_redraft_trace_count: int = Field(default=0)
    # Count of consecutive REQUEST_CHANGES verdicts inside the current
    # review session.  Reset on APPROVE or when the cap is hit; persists
    # across the CODE_REVIEW → READY → DOCUMENTING → CODE_REVIEW loop.
    review_rounds: int = Field(default=0)
    # total implement passes across all review rounds (ticket lifetime).
    # Used by the implement↔review convergence backstop to catch a ticket
    # that keeps re-running implement without converging.
    implement_cycles: int = Field(default=0)
    # count of refine passes for this ticket (lifetime). Feeds the
    # refine convergence backstop — when this reaches max_refine_passes_per_ticket
    # without convergence, the ticket is escalated to BLOCKED.
    refine_passes: int = Field(default=0)
    # hash of description.md output from the most recent refine pass.
    # Compared against the next pass's output to detect convergence
    # (unchanged output → the loop has stabilised).
    refine_output_hash: str = ""
    # transient-error retry state (stage-runner level, not LLM-call level)
    retry_attempt: int = Field(default=0)
    last_transient_error: str | None = Field(default=None)
    next_retry_at: datetime | None = Field(
        default=None, sa_column=Column(TZDateTime(), nullable=True)
    )
    # optional JSON list of ticket IDs that must reach CLOSED/DONE before
    # this ticket can leave READY (implement-stage gate).
    depends_on: str | None = Field(default=None)
    # optional JSON list of ticket IDs to AUTO-UNBLOCK when THIS ticket
    # completes (reaches DONE/CLOSED/EPIC_CLOSED). Each listed ticket that is
    # currently BLOCKED is transitioned BLOCKED -> DRAFT. The inverse of
    # depends_on, declared on the *solver*: "merging me re-opens these".
    # Cross-board safe (targets resolved via _board_for).
    unblocks: str | None = Field(default=None)
    # optional JSON list of free-form label strings applied to the ticket.
    labels: str | None = Field(default=None)
    # optional per-ticket override of the coordinator tool-call budget cap
    # (``coordinator_max_tool_calls``, default 300). When set, the effective
    # cap for implement and ci_fix coordinator passes becomes
    # ``min(override, coordinator_max_tool_calls_hard_cap)`` so a known-large
    # ticket can run above the default while the global hard backstop still
    # catches runaway loops. None = use the default cap (ordinary tickets).
    coordinator_max_tool_calls_override: int | None = Field(default=None)
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
    """Append-only state-transition history with hash-chain integrity."""

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: str = Field(foreign_key="ticket.id", index=True)
    state: State
    note: str | None = None
    at: datetime = Field(
        default_factory=_now,
        sa_column=Column(TZDateTime()),
    )
    prev_hash: str | None = Field(default=None)
    hash: str = Field(default="")


class Comment(SQLModel, table=True):
    """Reviewer comment on a ticket — supports threading via parent_id
    and open/closed tracking on top-level threads via closed_at.
    """

    id: int | None = Field(default=None, primary_key=True)
    ticket_id: str = Field(foreign_key="ticket.id", index=True)
    body: str
    author: str = Field(default="user")
    parent_id: int | None = Field(default=None, foreign_key="comment.id", nullable=True)
    closed_at: datetime | None = Field(
        default=None, sa_column=Column(TZDateTime(), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=_now,
        sa_column=Column(TZDateTime()),
    )


# --- API request/response shapes ---


class TicketCreate(SQLModel):
    """API request shape for creating a new ticket."""

    title: str = Field(description="User-provided ticket title.")
    description: str = Field(
        default="", description="Ticket spec body (Markdown); empty for a stub."
    )
    depends_on: str | None = Field(
        default=None,
        description="JSON array of ticket IDs this ticket depends on; None for none.",
    )
    # Ticket IDs to auto-unblock (BLOCKED -> DRAFT) when this ticket
    # completes. Accepts a JSON array of IDs.
    unblocks: list[str] | None = Field(
        default=None,
        description=(
            "Ticket IDs to auto-unblock (BLOCKED -> DRAFT) when this ticket completes."
        ),
    )
    source: str = Field(
        default=SourceKind.USER,
        description="Origin of the ticket (e.g. 'user', 'retrospect', 'audit').",
    )
    kind: TicketKind = Field(
        default=TicketKind.TASK, description="Ticket kind: task, inquiry, or epic."
    )
    parent_id: str | None = Field(
        default=None, description="ID of the parent epic ticket, if any."
    )
    repo_id: str | None = Field(
        default=None,
        description="Target board/repo ID; defaults to the current board when None.",
    )


class TicketTransition(SQLModel):
    """API request shape for transitioning a ticket to a new state."""

    state: State = Field(description="Target state to transition the ticket into.")
    note: str | None = Field(
        default=None,
        description="Optional human-readable note recorded with the transition.",
    )


class TicketMigrate(SQLModel):
    """API request shape for migrating a ticket to another board."""

    repo_id: str = Field(
        description="Destination board/repo ID to migrate the ticket to."
    )
    note: str | None = Field(
        default=None, description="Optional note recorded with the migration."
    )


class TicketDescriptionUpdate(SQLModel):
    """API request shape for updating a ticket's spec description."""

    description: str = Field(description="New ticket spec body (Markdown).")
    reset_fingerprint_guard: bool = Field(
        default=False,
        description="When True, reset the content-fingerprint guard that skips no-op redrafts.",
    )
    author: str = Field(
        default="operator", description="Who authored the description update."
    )


class TicketRead(SQLModel):
    """API response shape for reading a ticket, including computed fields like unmet_deps and PR URL."""

    id: str = Field(description="Unique ticket identifier.")
    title: str = Field(description="User-provided ticket title.")
    state: State = Field(description="Current state in the pipeline state machine.")
    kind: str = Field(description="Ticket kind: task, inquiry, or epic.")
    branch: str | None = Field(
        description="Git branch created by the implement stage; None before implementation."
    )
    parent_id: str | None = Field(description="ID of the parent epic ticket, if any.")
    parent_title: str | None = Field(
        default=None, description="Title of the parent epic ticket, if any."
    )
    source: str = Field(description="Origin of the ticket (e.g. 'user', 'retrospect').")
    origin_session: str | None = Field(
        description="Langfuse session ID that produced this ticket; None for human/API-created."
    )
    origin_session_url: str | None = Field(
        description="Langfuse UI URL for the originating session; None when unavailable."
    )
    cost_usd: float = Field(
        description="Cumulative LLM spend in USD, synced from Langfuse session totals."
    )
    pre_redraft_cost_usd: float = Field(
        default=0.0,
        description="Session cost snapshot at the last redraft; subtracted to yield per-attempt cost.",
    )
    pre_redraft_trace_count: int = Field(
        default=0,
        description="Session trace-count snapshot at the last trace-breaker resume; -1 sentinel when unset.",
    )
    cumulative_cost: float | None = Field(
        default=None,
        description="Total spend including child tickets for an epic; None for leaf tickets.",
    )
    depends_on: str | None = Field(
        description="Legacy JSON string of dependency ticket IDs; see 'dependencies' for resolved status."
    )
    unmet_deps: list[str] = Field(
        description="Ticket IDs this ticket depends on that are not yet CLOSED/DONE."
    )
    # Ticket IDs this ticket auto-unblocks on completion (parsed list).
    unblocks: list[str] = Field(
        default=[],
        description="Ticket IDs this ticket auto-unblocks (BLOCKED -> DRAFT) on completion.",
    )
    # Resolved per-dependency status for drawer display. Each entry is
    # ``{id, title, state}``; populated by ``enrich_ticket_read`` from
    # the parsed ``depends_on`` list. Empty list when the ticket has
    # no dependencies. The legacy ``depends_on`` JSON string and
    # ``unmet_deps`` ID list are kept for back-compat (the worker
    # gate keys off ``unmet_deps``).
    dependencies: list[dict[str, Any]] = Field(
        default=[],
        description="Resolved per-dependency status; each entry is {id, title, state}.",
    )
    pr_url: str | None = Field(
        default=None,
        description="URL of the pull request opened for this ticket; None if none.",
    )
    retry_attempt: int = Field(
        description="Number of transient-failure retry attempts made for the current stage."
    )
    last_transient_error: str | None = Field(
        description="Message of the most recent transient error; None when no retry pending."
    )
    next_retry_at: datetime | None = Field(
        description="Scheduled time of the next retry; None when no retry is pending."
    )
    priority: bool = Field(
        default=False,
        description="When True, the worker dequeues this ticket ahead of others.",
    )
    # per-ticket override of the coordinator tool-call budget cap; None when
    # the ticket uses the default cap.
    coordinator_max_tool_calls_override: int | None = Field(
        default=None,
        description="Per-ticket override of the coordinator tool-call budget cap; None uses the default.",
    )
    board_id: str = Field(
        default="", description="ID of the board/repo this ticket belongs to."
    )
    created_at: datetime = Field(description="Timestamp when the ticket was created.")
    updated_at: datetime = Field(description="Timestamp of the last modification.")
    # verbatim clarifying question from the latest open [ASK_USER]
    # comment when paused; None otherwise
    pending_question: str | None = Field(
        default=None,
        description="Clarifying question from the latest open [ASK_USER] comment when paused; None otherwise.",
    )


class CommentCreate(SQLModel):
    """API request shape for creating a comment (optionally threaded via parent_id)."""

    body: str = Field(description="Comment text body.")
    author: str = Field(default="user", description="Who authored the comment.")
    parent_id: int | None = Field(
        default=None,
        description="ID of the comment this one replies to; None for a top-level comment.",
    )


class CommentRead(SQLModel):
    """API response shape for reading a comment."""

    id: int = Field(description="Unique comment identifier.")
    ticket_id: str = Field(description="ID of the ticket this comment belongs to.")
    body: str = Field(description="Comment text body.")
    author: str = Field(description="Who authored the comment.")
    parent_id: int | None = Field(
        description="ID of the parent comment in a thread; None for a top-level comment."
    )
    closed_at: datetime | None = Field(
        description="Timestamp when the comment thread was closed/resolved; None if open."
    )
    created_at: datetime = Field(description="Timestamp when the comment was created.")


# --- Agent memory ledger (DB-backed, with retention) ---


class Memory(SQLModel, table=True):
    """Per-board, per-agent memory ledger row.

    Replaces the file-based Markdown ledger with a DB-backed table so
    retention is enforced and the full file is never injected unbounded
    into agent prompts.  Each row is one agent's ledger for one board.
    """

    id: int | None = Field(default=None, primary_key=True)
    board_id: str = Field(index=True)
    name: str = Field(index=True)
    content: str = Field(default="")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
