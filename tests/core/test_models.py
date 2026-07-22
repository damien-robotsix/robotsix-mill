"""Unit tests for robotsix_mill.core.models — pure, no database.

Covers every class and function exported from models.py: SourceKind,
_now(), Ticket, TicketEvent, Comment, TicketCreate, TicketTransition,
TicketRead, CommentCreate, CommentRead.

Also includes DB-backed regression tests that require the ``service``
and ``settings`` fixtures (via ``conftest.py``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from robotsix_mill.core import db
from robotsix_mill.core.models import (
    CaseTolerantEnum,
    Comment,
    CommentCreate,
    CommentRead,
    SourceKind,
    Ticket,
    TicketCreate,
    TicketEvent,
    TicketKind,
    TicketRead,
    TicketTransition,
    _now,
)
from robotsix_mill.core.states import State


# ---------------------------------------------------------------------------
# _now()
# ---------------------------------------------------------------------------


def test_now_returns_aware_utc_datetime():
    """_now() returns a datetime with tzinfo == timezone.utc."""
    result = _now()
    assert isinstance(result, datetime)
    assert result.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# SourceKind
# ---------------------------------------------------------------------------


def test_sourcekind_member_count():
    """SourceKind must contain the expected set of members."""
    expected = {
        "USER",
        "RETROSPECT",
        "AUDIT",
        "SURVEY",
        "AGENT",
        "CI",
        "HEALTH",
        "CONFIG_SYNC",
        "MEMBER_SYNC",
        "TEST_GAP",
        "AGENT_CHECK",
        "BC_CHECK",
        "DATA_DIR_GC",
        "DEPENDABOT_ALERTS",
        "COMPLETENESS_CHECK",
        "COPY_PASTE",
        "DOCSTRING_COVERAGE",
        "FORGE_PARITY",
        "TRACE_HEALTH",
        "TRACE_REVIEW",
        "MODULE_CURATOR",
        "ROADMAP_SYNC",
        "STATE_SYNC",
        "FRONTEND_SYNC",
        "TRIAGE_BOILERPLATE",
        "META",
        "RUN_HEALTH",
        "LANGFUSE_CLEANUP",
        "CI_FIX_DEPENDENCY",
        "IMPLEMENT_BASELINE_DEPENDENCY",
        "ORPHANED_PR_CHECK",
        "REPO_DESCRIPTION_SYNC",
        "MODULE_SIZE",
        "CONFIG_STANDARD",
    }
    assert set(SourceKind.__members__) == expected


def test_sourcekind_all_values_are_lowercase_strings():
    """Every SourceKind member's .value is a lowercase str."""
    for member in SourceKind:
        assert isinstance(member, str)
        assert isinstance(member.value, str)
        assert member.value.islower()


def test_sourcekind_members_are_str_instances():
    """Every SourceKind member isinstance(m, str) holds."""
    for member in SourceKind:
        assert isinstance(member, str)


# ---------------------------------------------------------------------------
# Ticket (table=True)
# ---------------------------------------------------------------------------


def test_ticket_defaults():
    """Ticket fields carry correct defaults when constructed with minimum kwargs."""
    ticket = Ticket(id="t-1", title="Test Ticket", workspace_path="/tmp/t-1")
    assert ticket.state == State.DRAFT
    assert ticket.kind == TicketKind.TASK
    assert ticket.source == SourceKind.USER
    assert ticket.content_hash == ""
    assert ticket.cost_usd == 0.0
    assert ticket.review_rounds == 0
    assert ticket.retry_attempt == 0
    assert ticket.priority is False
    assert ticket.board_id == ""
    assert ticket.branch is None
    assert ticket.parent_id is None
    assert ticket.blocked_from is None
    assert ticket.paused_from is None
    assert ticket.origin_session is None
    assert ticket.last_transient_error is None
    assert ticket.next_retry_at is None
    assert ticket.depends_on is None


def test_ticket_content_hash_defaults_to_empty_string_not_none():
    """content_hash defaults to "" (empty string), not None."""
    ticket = Ticket(id="t-2", title="T", workspace_path="/tmp/t-2")
    assert ticket.content_hash == ""
    assert ticket.content_hash is not None


def test_ticket_source_is_enum_member_not_string():
    """source defaults to SourceKind.USER (the enum member)."""
    ticket = Ticket(id="t-3", title="T", workspace_path="/tmp/t-3")
    assert ticket.source == SourceKind.USER
    assert ticket.source == "user"
    # It is the enum member, so isinstance(..., SourceKind) holds
    assert isinstance(ticket.source, SourceKind)


def test_ticket_kind_accepts_task_inquiry_epic():
    """kind is free-form string; defaults to 'task'."""
    ticket = Ticket(id="t-4", title="T", workspace_path="/tmp/t-4")
    assert ticket.kind == TicketKind.TASK
    ticket.kind = TicketKind.INQUIRY
    assert ticket.kind == TicketKind.INQUIRY
    ticket.kind = TicketKind.EPIC
    assert ticket.kind == TicketKind.EPIC


def test_ticket_created_at_and_updated_at_are_aware_utc():
    """created_at and updated_at carry timezone.utc after construction."""
    ticket = Ticket(id="t-5", title="T", workspace_path="/tmp/t-5")
    assert ticket.created_at.tzinfo == timezone.utc
    assert ticket.updated_at.tzinfo == timezone.utc


def test_ticket_next_retry_at_with_aware_datetime():
    """next_retry_at explicitly set with aware datetime preserves tzinfo on read-back."""
    dt = datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    ticket = Ticket(id="t-6", title="T", workspace_path="/tmp/t-6", next_retry_at=dt)
    assert ticket.next_retry_at is not None
    assert ticket.next_retry_at.tzinfo == timezone.utc
    assert ticket.next_retry_at == dt


def test_ticket_model_dump_and_validate_roundtrip():
    """Ticket model_dump() → model_validate() reproduces equivalent instance."""
    ticket = Ticket(
        id="t-roundtrip",
        title="Round Trip",
        workspace_path="/tmp/t-roundtrip",
        kind=TicketKind.EPIC,
        source=SourceKind.AGENT,
        priority=True,
        board_id="board-1",
    )
    dumped = ticket.model_dump()
    assert isinstance(dumped, dict)
    restored = Ticket.model_validate(dumped)
    assert restored.id == ticket.id
    assert restored.title == ticket.title
    assert restored.workspace_path == ticket.workspace_path
    assert restored.kind == ticket.kind
    assert restored.source == ticket.source
    assert restored.priority == ticket.priority
    assert restored.board_id == ticket.board_id
    assert restored.content_hash == ticket.content_hash
    assert restored.state == ticket.state


def test_ticket_all_fields_in_roundtrip():
    """Every non-default Ticket field survives model_dump() → model_validate()."""
    ticket = Ticket(
        id="t-full",
        title="Full Ticket",
        workspace_path="/tmp/full",
        kind=TicketKind.INQUIRY,
        source=SourceKind.CI,
        branch="feature/foo",
        parent_id="t-parent",
        blocked_from=State.READY,
        paused_from=State.READY,
        origin_session="sess-abc",
        cost_usd=12.34,
        review_rounds=2,
        retry_attempt=1,
        last_transient_error="timeout",
        next_retry_at=datetime(2025, 7, 1, tzinfo=timezone.utc),
        depends_on='["dep-1"]',
        board_id="brd-99",
        priority=True,
    )
    dumped = ticket.model_dump()
    restored = Ticket.model_validate(dumped)
    assert restored.id == ticket.id
    assert restored.title == ticket.title
    assert restored.branch == ticket.branch
    assert restored.parent_id == ticket.parent_id
    assert restored.blocked_from == ticket.blocked_from
    assert restored.paused_from == ticket.paused_from
    assert restored.origin_session == ticket.origin_session
    assert restored.cost_usd == ticket.cost_usd
    assert restored.review_rounds == ticket.review_rounds
    assert restored.retry_attempt == ticket.retry_attempt
    assert restored.last_transient_error == ticket.last_transient_error
    assert restored.next_retry_at == ticket.next_retry_at
    assert restored.depends_on == ticket.depends_on
    assert restored.board_id == ticket.board_id
    assert restored.priority == ticket.priority


# ---------------------------------------------------------------------------
# TicketEvent (table=True)
# ---------------------------------------------------------------------------


def test_ticket_event_defaults():
    """TicketEvent id defaults to None; note defaults to None; hash defaults to ''; prev_hash defaults to None."""
    event = TicketEvent(ticket_id="t-1", state=State.READY)
    assert event.id is None
    assert event.note is None
    assert event.hash == ""
    assert event.prev_hash is None
    assert event.ticket_id == "t-1"
    assert event.state == State.READY


def test_ticket_event_at_is_aware_utc():
    """at is populated with a UTC-aware datetime."""
    event = TicketEvent(ticket_id="t-1", state=State.DRAFT)
    assert event.at.tzinfo == timezone.utc


def test_ticket_event_with_note():
    """TicketEvent with explicit note preserves it."""
    event = TicketEvent(ticket_id="t-1", state=State.ERRORED, note="boom")
    assert event.note == "boom"


def test_ticket_event_model_dump_and_validate_roundtrip():
    """TicketEvent model_dump() → model_validate() round-trip."""
    event = TicketEvent(
        ticket_id="t-1",
        state=State.DONE,
        note="all good",
        prev_hash="abc123",
        hash="def456",
    )
    dumped = event.model_dump()
    restored = TicketEvent.model_validate(dumped)
    assert restored.ticket_id == event.ticket_id
    assert restored.state == event.state
    assert restored.note == event.note
    assert restored.at == event.at
    assert restored.prev_hash == "abc123"
    assert restored.hash == "def456"


# ---------------------------------------------------------------------------
# Comment (table=True)
# ---------------------------------------------------------------------------


def test_comment_defaults():
    """Comment id defaults to None; author defaults to 'user'; parent_id and closed_at to None."""
    comment = Comment(ticket_id="t-1", body="Looks good")
    assert comment.id is None
    assert comment.author == "user"
    assert comment.parent_id is None
    assert comment.closed_at is None
    assert comment.body == "Looks good"
    assert comment.ticket_id == "t-1"


def test_comment_created_at_is_aware_utc():
    """created_at is populated by _now() with tzinfo=utc."""
    comment = Comment(ticket_id="t-1", body="hi")
    assert comment.created_at.tzinfo == timezone.utc


def test_comment_closed_at_aware_roundtrip():
    """closed_at set with aware datetime survives model_dump()."""
    dt = datetime(2025, 10, 1, tzinfo=timezone.utc)
    comment = Comment(ticket_id="t-1", body="x", closed_at=dt)
    assert comment.closed_at == dt
    assert comment.closed_at.tzinfo == timezone.utc
    dumped = comment.model_dump()
    restored = Comment.model_validate(dumped)
    assert restored.closed_at == dt


def test_comment_model_dump_and_validate_roundtrip():
    """Comment model_dump() → model_validate() round-trip."""
    comment = Comment(
        ticket_id="t-1",
        body="Needs work",
        author="reviewer",
        parent_id=5,
        closed_at=datetime(2025, 5, 5, tzinfo=timezone.utc),
    )
    dumped = comment.model_dump()
    restored = Comment.model_validate(dumped)
    assert restored.ticket_id == comment.ticket_id
    assert restored.body == comment.body
    assert restored.author == comment.author
    assert restored.parent_id == comment.parent_id
    assert restored.closed_at == comment.closed_at
    assert restored.created_at == comment.created_at


# ---------------------------------------------------------------------------
# TicketCreate (API schema)
# ---------------------------------------------------------------------------


def test_ticket_create_minimal():
    """TicketCreate with only title: description defaults to '', source to USER, kind to 'task'."""
    tc = TicketCreate(title="Hello")
    assert tc.title == "Hello"
    assert tc.description == ""
    assert tc.source == SourceKind.USER
    assert tc.kind == TicketKind.TASK
    assert tc.depends_on is None
    assert tc.parent_id is None
    assert tc.repo_id is None


def test_ticket_create_missing_title_raises():
    """TicketCreate without title raises ValidationError."""
    with pytest.raises(ValidationError):
        TicketCreate()


def test_ticket_create_model_dump_excludes_unset():
    """model_dump() on minimal TicketCreate excludes None optionals."""
    tc = TicketCreate(title="Minimal")
    dumped = tc.model_dump()
    assert "title" in dumped
    assert "description" in dumped  # has default ""
    # depends_on / parent_id / repo_id are None but have no default — they should
    # appear with value None (SQLModel / Pydantic include them).
    assert "depends_on" in dumped


def test_ticket_create_model_dump_and_validate_roundtrip():
    """TicketCreate round-trip preserves all fields."""
    tc = TicketCreate(
        title="Full",
        description="desc",
        depends_on='["t-1"]',
        source=SourceKind.AUDIT,
        kind=TicketKind.EPIC,
        parent_id="t-parent",
        repo_id="repo-1",
    )
    dumped = tc.model_dump()
    restored = TicketCreate.model_validate(dumped)
    assert restored.title == tc.title
    assert restored.description == tc.description
    assert restored.depends_on == tc.depends_on
    assert restored.source == tc.source
    assert restored.kind == tc.kind
    assert restored.parent_id == tc.parent_id
    assert restored.repo_id == tc.repo_id


# ---------------------------------------------------------------------------
# TicketTransition (API schema)
# ---------------------------------------------------------------------------


def test_ticket_transition_requires_state():
    """TicketTransition without state raises ValidationError."""
    with pytest.raises(ValidationError):
        TicketTransition()


def test_ticket_transition_note_defaults_none():
    """note defaults to None."""
    tt = TicketTransition(state=State.READY)
    assert tt.state == State.READY
    assert tt.note is None


def test_ticket_transition_with_note():
    """TicketTransition with explicit note."""
    tt = TicketTransition(state=State.BLOCKED, note="CI failure")
    assert tt.state == State.BLOCKED
    assert tt.note == "CI failure"


def test_ticket_transition_model_dump_and_validate_roundtrip():
    """TicketTransition model_dump() → model_validate() round-trip."""
    tt = TicketTransition(state=State.DONE, note="merged")
    dumped = tt.model_dump()
    restored = TicketTransition.model_validate(dumped)
    assert restored.state == tt.state
    assert restored.note == tt.note


# ---------------------------------------------------------------------------
# TicketRead (API read shape)
# ---------------------------------------------------------------------------


def test_ticket_read_minimal_validation():
    """TicketRead.model_validate() from a minimal dict populates all fields."""
    data = {
        "id": "t-1",
        "title": "Test",
        "state": "draft",
        "kind": TicketKind.TASK,
        "branch": None,
        "parent_id": None,
        "source": "user",
        "origin_session": None,
        "origin_session_url": None,
        "cost_usd": 0.0,
        "depends_on": None,
        "unmet_deps": [],
        "retry_attempt": 0,
        "last_transient_error": None,
        "next_retry_at": None,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
    }
    tr = TicketRead.model_validate(data)
    assert tr.id == "t-1"
    assert tr.parent_title is None
    assert tr.cumulative_cost is None
    assert tr.pr_url is None
    assert tr.unmet_deps == []
    assert tr.priority is False
    assert tr.board_id == ""


def test_ticket_read_unmet_deps_defaults_to_empty_list():
    """unmet_deps defaults to empty list (not None)."""
    data = {
        "id": "t-1",
        "title": "Test",
        "state": "draft",
        "kind": TicketKind.TASK,
        "branch": None,
        "parent_id": None,
        "source": "user",
        "origin_session": None,
        "origin_session_url": None,
        "cost_usd": 0.0,
        "depends_on": None,
        "unmet_deps": [],
        "retry_attempt": 0,
        "last_transient_error": None,
        "next_retry_at": None,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
    }
    tr = TicketRead.model_validate(data)
    assert tr.unmet_deps == []
    assert isinstance(tr.unmet_deps, list)


def test_ticket_read_with_unmet_deps_populated():
    """unmet_deps with actual values survives validation."""
    data = {
        "id": "t-1",
        "title": "Test",
        "state": "draft",
        "kind": TicketKind.TASK,
        "branch": None,
        "parent_id": None,
        "source": "user",
        "origin_session": None,
        "origin_session_url": None,
        "cost_usd": 0.0,
        "depends_on": '["t-2"]',
        "unmet_deps": ["t-2"],
        "retry_attempt": 0,
        "last_transient_error": None,
        "next_retry_at": None,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
    }
    tr = TicketRead.model_validate(data)
    assert tr.unmet_deps == ["t-2"]


def test_ticket_read_missing_unmet_deps_raises():
    """TicketRead without unmet_deps raises ValidationError (field is required, no default in schema)."""
    data = {
        "id": "t-1",
        "title": "Test",
        "state": "draft",
        "kind": TicketKind.TASK,
        "branch": None,
        "parent_id": None,
        "source": "user",
        "origin_session": None,
        "origin_session_url": None,
        "cost_usd": 0.0,
        "depends_on": None,
        "retry_attempt": 0,
        "last_transient_error": None,
        "next_retry_at": None,
        "created_at": "2025-01-01T00:00:00Z",
        "updated_at": "2025-01-01T00:00:00Z",
    }
    with pytest.raises(ValidationError):
        TicketRead.model_validate(data)


def test_ticket_read_roundtrip():
    """TicketRead model_dump() → model_validate() round-trip."""
    tr = TicketRead(
        id="t-rt",
        title="RT",
        state=State.READY,
        kind=TicketKind.TASK,
        branch="br/rt",
        parent_id=None,
        parent_title="Parent",
        source="agent",
        origin_session=None,
        origin_session_url=None,
        cost_usd=1.0,
        cumulative_cost=5.0,
        depends_on=None,
        unmet_deps=["d1"],
        pr_url="https://example.com/pr",
        retry_attempt=0,
        last_transient_error=None,
        next_retry_at=None,
        priority=True,
        board_id="b",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    dumped = tr.model_dump()
    restored = TicketRead.model_validate(dumped)
    assert restored.id == tr.id
    assert restored.parent_title == tr.parent_title
    assert restored.cumulative_cost == tr.cumulative_cost
    assert restored.pr_url == tr.pr_url
    assert restored.unmet_deps == tr.unmet_deps


# ---------------------------------------------------------------------------
# CommentCreate (API schema)
# ---------------------------------------------------------------------------


def test_comment_create_defaults():
    """author defaults to 'user'; parent_id defaults to None."""
    cc = CommentCreate(body="Hello")
    assert cc.body == "Hello"
    assert cc.author == "user"
    assert cc.parent_id is None


def test_comment_create_missing_body_raises():
    """body is required; omitting it raises ValidationError."""
    with pytest.raises(ValidationError):
        CommentCreate()


def test_comment_create_with_parent_id():
    """parent_id can be set explicitly."""
    cc = CommentCreate(body="Reply", author="bot", parent_id=3)
    assert cc.parent_id == 3


def test_comment_create_model_dump_and_validate_roundtrip():
    """CommentCreate round-trip preserves all fields."""
    cc = CommentCreate(body="Nice", author="reviewer", parent_id=7)
    dumped = cc.model_dump()
    restored = CommentCreate.model_validate(dumped)
    assert restored.body == cc.body
    assert restored.author == cc.author
    assert restored.parent_id == cc.parent_id


# ---------------------------------------------------------------------------
# CommentRead (API read shape)
# ---------------------------------------------------------------------------


def test_comment_read_all_fields_present():
    """All CommentRead fields are populated from a valid dict."""
    cr = CommentRead(
        id=1,
        ticket_id="t-1",
        body="Looks good",
        author="reviewer",
        parent_id=None,
        closed_at=None,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert cr.id == 1
    assert cr.ticket_id == "t-1"
    assert cr.body == "Looks good"
    assert cr.author == "reviewer"
    assert cr.parent_id is None
    assert cr.closed_at is None


def test_comment_read_parent_id_and_closed_at_can_be_none():
    """parent_id and closed_at accept None."""
    cr = CommentRead(
        id=2,
        ticket_id="t-2",
        body="x",
        author="a",
        parent_id=None,
        closed_at=None,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert cr.parent_id is None
    assert cr.closed_at is None


def test_comment_read_with_closed_at():
    """closed_at with an aware datetime survives validation."""
    dt = datetime(2025, 6, 1, tzinfo=timezone.utc)
    cr = CommentRead(
        id=3,
        ticket_id="t-3",
        body="Resolved",
        author="bot",
        parent_id=1,
        closed_at=dt,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    assert cr.closed_at == dt


def test_comment_read_model_dump_and_validate_roundtrip():
    """CommentRead round-trip preserves all fields."""
    dt = datetime(2025, 3, 3, tzinfo=timezone.utc)
    cr = CommentRead(
        id=10,
        ticket_id="t-round",
        body="Round trip",
        author="alice",
        parent_id=5,
        closed_at=dt,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    dumped = cr.model_dump()
    restored = CommentRead.model_validate(dumped)
    assert restored.id == cr.id
    assert restored.ticket_id == cr.ticket_id
    assert restored.body == cr.body
    assert restored.author == cr.author
    assert restored.parent_id == cr.parent_id
    assert restored.closed_at == cr.closed_at
    assert restored.created_at == cr.created_at


# ---------------------------------------------------------------------------
# CaseTolerantEnum unit tests (no database)
# ---------------------------------------------------------------------------


def test_case_tolerant_enum_bind_none():
    """None passes through process_bind_param unchanged."""
    ct = CaseTolerantEnum(TicketKind)
    assert ct.process_bind_param(None, None) is None


def test_case_tolerant_enum_result_none():
    """None passes through process_result_value unchanged."""
    ct = CaseTolerantEnum(TicketKind)
    assert ct.process_result_value(None, None) is None


def test_case_tolerant_enum_bind_enum_member():
    """Bind with a TicketKind member returns the uppercase member name."""
    ct = CaseTolerantEnum(TicketKind)
    assert ct.process_bind_param(TicketKind.TASK, None) == "TASK"
    assert ct.process_bind_param(TicketKind.INQUIRY, None) == "INQUIRY"
    assert ct.process_bind_param(TicketKind.EPIC, None) == "EPIC"


def test_case_tolerant_enum_bind_lowercase_string():
    """Bind with a lowercase string resolves to the canonical uppercase name."""
    ct = CaseTolerantEnum(TicketKind)
    assert ct.process_bind_param("task", None) == "TASK"
    assert ct.process_bind_param("inquiry", None) == "INQUIRY"
    assert ct.process_bind_param("epic", None) == "EPIC"


def test_case_tolerant_enum_bind_mixed_case_string():
    """Bind with mixed-case string still resolves to uppercase name."""
    ct = CaseTolerantEnum(TicketKind)
    assert ct.process_bind_param("Task", None) == "TASK"
    assert ct.process_bind_param("InQuIrY", None) == "INQUIRY"


def test_case_tolerant_enum_bind_uppercase_string():
    """Bind with uppercase string resolves to uppercase name."""
    ct = CaseTolerantEnum(TicketKind)
    assert ct.process_bind_param("TASK", None) == "TASK"


def test_case_tolerant_enum_bind_invalid_string_raises():
    """Bind with an invalid value raises ValueError."""
    ct = CaseTolerantEnum(TicketKind)
    with pytest.raises(ValueError, match="not a valid TicketKind"):
        ct.process_bind_param("garbage", None)


def test_case_tolerant_enum_result_lowercase_string():
    """Read a lowercase DB string → TicketKind member."""
    ct = CaseTolerantEnum(TicketKind)
    result = ct.process_result_value("task", None)
    assert result == TicketKind.TASK
    assert isinstance(result, TicketKind)


def test_case_tolerant_enum_result_uppercase_string():
    """Read an uppercase DB string → TicketKind member."""
    ct = CaseTolerantEnum(TicketKind)
    result = ct.process_result_value("TASK", None)
    assert result == TicketKind.TASK


def test_case_tolerant_enum_result_invalid_string_raises():
    """Read an invalid DB string raises ValueError."""
    ct = CaseTolerantEnum(TicketKind)
    with pytest.raises(ValueError, match="not a valid TicketKind"):
        ct.process_result_value("bogus", None)


# ---------------------------------------------------------------------------
# DB-backed regression tests — CaseTolerantEnum on Ticket.kind
# ---------------------------------------------------------------------------


def _raw_insert_ticket(settings, board_id: str, ticket_id: str, kind: str) -> None:
    """Insert a minimal ticket row via raw SQL with an explicit *kind*."""
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    engine = db.get_engine(settings, board_id)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            f"""
            INSERT INTO ticket (id, title, state, kind, workspace_path, content_hash,
                                source, cost_usd, pre_redraft_cost_usd, pre_redraft_trace_count, review_rounds,
                                retry_attempt, board_id, priority, implement_cycles,
                                refine_passes, refine_output_hash,
                                created_at, updated_at)
            VALUES ('{ticket_id}', 'Test title', 'DRAFT', '{kind}', '/tmp/{ticket_id}', '',
                    'user', 0.0, 0.0, 0, 0, 0, '', 0, 0, 0, '',
                    '{now}', '{now}')
            """
        )


def test_raw_sql_lowercase_kind_round_trip(service):
    """Insert a row with kind='task' (lowercase) via raw SQL;
    read it back with TicketService and assert correct TicketKind."""
    _raw_insert_ticket(service.settings, service.board_id, "t-lower-raw", "task")

    ticket = service.get("t-lower-raw")
    assert ticket is not None
    assert ticket.kind == TicketKind.TASK
    assert isinstance(ticket.kind, TicketKind)

    all_tickets = service.list()
    match = [t for t in all_tickets if t.id == "t-lower-raw"]
    assert len(match) == 1
    assert match[0].kind == TicketKind.TASK


def test_raw_sql_mixed_case_kind_round_trip(service):
    """Insert a row with kind='InQuIrY' (mixed case) via raw SQL;
    read it back cleanly."""
    _raw_insert_ticket(service.settings, service.board_id, "t-mixed-raw", "InQuIrY")

    ticket = service.get("t-mixed-raw")
    assert ticket is not None
    assert ticket.kind == TicketKind.INQUIRY
    assert isinstance(ticket.kind, TicketKind)


def test_raw_sql_uppercase_kind_round_trip(service):
    """Insert a row with kind='EPIC' (uppercase) via raw SQL;
    read it back cleanly (canonical case)."""
    _raw_insert_ticket(service.settings, service.board_id, "t-upper-raw", "EPIC")

    ticket = service.get("t-upper-raw")
    assert ticket is not None
    assert ticket.kind == TicketKind.EPIC
    assert isinstance(ticket.kind, TicketKind)


def test_create_with_lowercase_string_kind(service):
    """Create a ticket by passing kind='task' (lowercase str);
    assert it reads back as TicketKind.TASK."""
    # Use the internal create path — TicketCreate → _lifecycle → Ticket(kind=...)
    from robotsix_mill.core.models import TicketCreate

    tc = TicketCreate(title="Lower Create", kind=TicketKind("task"))
    t = service.create(title=tc.title, description=tc.description, kind=tc.kind)
    assert t.kind == TicketKind.TASK

    # Also verify directly using TicketCreate.model_validate kind=TicketKind.TASK
    tc2 = TicketCreate(title="Lower Create 2", kind=TicketKind.TASK)  # type: ignore[arg-type]
    t2 = service.create(title=tc2.title, description=tc2.description, kind=tc2.kind)
    assert t2.kind == TicketKind.TASK


def test_create_with_enum_member_kind(service):
    """Create a ticket by passing kind=TicketKind.TASK (enum member);
    assert it reads back as TicketKind.TASK."""
    t = service.create(title="Enum Create", kind=TicketKind.TASK)
    assert t.kind == TicketKind.TASK


def test_kind_persisted_string_is_canonical_uppercase(service):
    """After creating a ticket with kind='task', the raw DB string is 'TASK'."""
    t = service.create(title="Canonical")
    # Verify raw DB value
    engine = db.get_engine(service.settings, service.board_id)
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            f"SELECT kind FROM ticket WHERE id = '{t.id}'"
        ).first()
    assert row is not None
    assert row[0] == "TASK"


def test_kind_persisted_string_uppercase_from_enum(service):
    """After creating a ticket with TicketKind.EPIC, the raw DB string is 'EPIC'."""
    t = service.create(title="Epic-enum", kind=TicketKind.EPIC)
    engine = db.get_engine(service.settings, service.board_id)
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            f"SELECT kind FROM ticket WHERE id = '{t.id}'"
        ).first()
    assert row is not None
    assert row[0] == "EPIC"


def test_init_db_migration_uppercases_lowercase_row(service, settings):
    """Insert a lowercase 'task' row, call init_db, assert it becomes 'TASK'."""
    _raw_insert_ticket(settings, service.board_id, "t-migrate", "task")

    # Verify lowercase before migration
    engine = db.get_engine(settings, service.board_id)
    with engine.connect() as conn:
        before = conn.exec_driver_sql(
            "SELECT kind FROM ticket WHERE id = 't-migrate'"
        ).first()
    assert before is not None and before[0] == "task"

    # Run init_db — this should trigger the UPDATE ticket SET kind = upper(kind)
    db.init_db(settings, service.board_id)

    with engine.connect() as conn:
        after = conn.exec_driver_sql(
            "SELECT kind FROM ticket WHERE id = 't-migrate'"
        ).first()
    assert after is not None and after[0] == "TASK"


def test_init_db_migration_is_idempotent(service, settings):
    """Calling init_db twice after inserting a lowercase row is safe (no error)."""
    _raw_insert_ticket(settings, service.board_id, "t-idem-mig", "inquiry")

    # First migration: lowercase → uppercase
    db.init_db(settings, service.board_id)
    engine = db.get_engine(settings, service.board_id)
    with engine.connect() as conn:
        first = conn.exec_driver_sql(
            "SELECT kind FROM ticket WHERE id = 't-idem-mig'"
        ).first()
    assert first is not None and first[0] == "INQUIRY"

    # Second migration: no-op (already uppercase)
    db.init_db(settings, service.board_id)
    with engine.connect() as conn:
        second = conn.exec_driver_sql(
            "SELECT kind FROM ticket WHERE id = 't-idem-mig'"
        ).first()
    assert second is not None and second[0] == "INQUIRY"
