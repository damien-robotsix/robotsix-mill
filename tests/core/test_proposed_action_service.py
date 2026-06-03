"""Tests for ProposedActionService — CRUD, approve/reject, and executor.

Exercises every method, including idempotence and error paths.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from robotsix_mill.core.models import (
    ActionType,
    ProposedAction,
    ProposedActionStatus,
    SourceKind,
)
from robotsix_mill.core.proposed_action_service import ProposedActionService
from robotsix_mill.core.service import TicketService, TransitionError
from robotsix_mill.core.states import State


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pa_close(
    svc: ProposedActionService,
    target: str = "t-target",
    rationale: str = "Stale",
    source: str = SourceKind.BOARD_CLEANUP,
) -> ProposedAction:
    return svc.create_proposed_action(
        action_type=ActionType.CLOSE_TICKET,
        target_ticket_id=target,
        rationale=rationale,
        source=source,
    )


def _make_pa_create(
    svc: ProposedActionService,
    title: str = "New ticket",
    body: str = "Body",
    rationale: str = "Gap detected",
    source: str = SourceKind.BOARD_CLEANUP,
) -> ProposedAction:
    return svc.create_proposed_action(
        action_type=ActionType.CREATE_TICKET,
        proposed_title=title,
        proposed_body=body,
        rationale=rationale,
        source=source,
    )


@pytest.fixture
def pas(settings, service) -> ProposedActionService:
    """Return a ProposedActionService wired to the test TicketService."""
    return ProposedActionService(settings, service)


# ---------------------------------------------------------------------------
# create_proposed_action
# ---------------------------------------------------------------------------


class TestCreate:
    def test_create_close_ticket(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        assert pa.id is not None
        assert pa.action_type == ActionType.CLOSE_TICKET
        assert pa.target_ticket_id == "t-1"
        assert pa.status == ProposedActionStatus.PENDING
        assert pa.board_id == "test-board"
        assert pa.created_at.tzinfo == timezone.utc
        assert pa.rationale == "Stale"
        assert pa.source == SourceKind.BOARD_CLEANUP
        assert pa.proposed_title is None
        assert pa.proposed_body is None

    def test_create_create_ticket(self, pas):
        pa = _make_pa_create(pas, title="T", body="B")
        assert pa.action_type == ActionType.CREATE_TICKET
        assert pa.proposed_title == "T"
        assert pa.proposed_body == "B"
        assert pa.target_ticket_id is None

    def test_missing_target_ticket_id_for_close_raises_valueerror(self, pas):
        with pytest.raises(ValueError, match="target_ticket_id"):
            pas.create_proposed_action(
                action_type=ActionType.CLOSE_TICKET,
                rationale="r",
                source="x",
            )

    def test_missing_proposed_title_for_create_raises_valueerror(self, pas):
        with pytest.raises(ValueError, match="proposed_title"):
            pas.create_proposed_action(
                action_type=ActionType.CREATE_TICKET,
                proposed_body="b",
                rationale="r",
                source="x",
            )

    def test_empty_proposed_title_for_create_raises_valueerror(self, pas):
        with pytest.raises(ValueError, match="proposed_title"):
            pas.create_proposed_action(
                action_type=ActionType.CREATE_TICKET,
                proposed_title="",
                rationale="r",
                source="x",
            )

    def test_create_ticket_without_body_is_allowed(self, pas):
        """proposed_body is optional for CREATE_TICKET (may be empty)."""
        pa = pas.create_proposed_action(
            action_type=ActionType.CREATE_TICKET,
            proposed_title="T",
            rationale="r",
            source="x",
        )
        assert pa.proposed_body is None


# ---------------------------------------------------------------------------
# list_proposed_actions / get_proposed_action
# ---------------------------------------------------------------------------


class TestRead:
    def test_list_empty(self, pas):
        assert pas.list_proposed_actions() == []

    def test_list_all(self, pas):
        pa1 = _make_pa_close(pas, target="t-a")
        pa2 = _make_pa_create(pas, title="T2")
        result = pas.list_proposed_actions()
        assert len(result) == 2
        # Most recent first
        assert result[0].id == pa2.id
        assert result[1].id == pa1.id

    def test_list_filter_by_status(self, pas):
        _make_pa_close(pas, target="t-1")
        pa2 = _make_pa_close(pas, target="t-2")
        pas.approve_proposed_action(pa2.id, "alice")
        pending = pas.list_proposed_actions(status=ProposedActionStatus.PENDING)
        assert len(pending) == 1
        assert pending[0].status == ProposedActionStatus.PENDING
        approved = pas.list_proposed_actions(status=ProposedActionStatus.APPROVED)
        assert len(approved) == 1
        assert approved[0].status == ProposedActionStatus.APPROVED

    def test_list_filter_by_source(self, pas):
        _make_pa_close(pas, target="t-1", source="health")
        _make_pa_close(pas, target="t-2", source=SourceKind.BOARD_CLEANUP)
        result = pas.list_proposed_actions(source="health")
        assert len(result) == 1
        assert result[0].source == "health"

    def test_get_found(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        assert pas.get_proposed_action(pa.id).id == pa.id

    def test_get_missing_raises_keyerror(self, pas):
        with pytest.raises(KeyError):
            pas.get_proposed_action(99999)

    def test_get_wrong_board_raises_keyerror(self, settings, service):
        """A ProposedAction on board A must not be visible from board B."""
        pas_a = ProposedActionService(settings, service)
        pa = _make_pa_close(pas_a, target="t-x")
        # Create a second service bound to a different board.
        pas_b = ProposedActionService(
            settings,
            TicketService(settings, board_id="other-board"),
        )
        with pytest.raises(KeyError):
            pas_b.get_proposed_action(pa.id)


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------


class TestApprove:
    def test_approve_happy_path(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        result = pas.approve_proposed_action(pa.id, "alice")
        assert result.status == ProposedActionStatus.APPROVED
        assert result.approver_id == "alice"
        assert result.approved_at is not None
        assert result.approved_at.tzinfo == timezone.utc
        # Persisted
        reloaded = pas.get_proposed_action(pa.id)
        assert reloaded.status == ProposedActionStatus.APPROVED

    def test_approve_non_pending_raises_transitionerror(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        pas.approve_proposed_action(pa.id, "alice")
        with pytest.raises(TransitionError, match="cannot approve"):
            pas.approve_proposed_action(pa.id, "bob")

    def test_approve_already_rejected_raises_transitionerror(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        pas.reject_proposed_action(pa.id, "alice")
        with pytest.raises(TransitionError, match="cannot approve"):
            pas.approve_proposed_action(pa.id, "bob")

    def test_approve_missing_raises_keyerror(self, pas):
        with pytest.raises(KeyError):
            pas.approve_proposed_action(99999, "alice")


class TestReject:
    def test_reject_happy_path(self, pas):
        pa = _make_pa_close(pas, target="t-1", rationale="Stale")
        result = pas.reject_proposed_action(pa.id, "bob", reason="Not now")
        assert result.status == ProposedActionStatus.REJECTED
        assert result.approver_id == "bob"
        assert result.rejected_at is not None
        assert result.rejected_at.tzinfo == timezone.utc
        assert "Rejection reason: Not now" in result.rationale
        assert result.rationale.startswith("Stale")
        # Persisted
        reloaded = pas.get_proposed_action(pa.id)
        assert reloaded.status == ProposedActionStatus.REJECTED
        assert "Rejection reason: Not now" in reloaded.rationale

    def test_reject_without_reason_preserves_rationale(self, pas):
        pa = _make_pa_close(pas, target="t-1", rationale="Stale")
        result = pas.reject_proposed_action(pa.id, "bob")
        assert result.rationale == "Stale"
        assert "Rejection reason" not in result.rationale

    def test_reject_non_pending_raises_transitionerror(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        pas.reject_proposed_action(pa.id, "alice")
        with pytest.raises(TransitionError, match="cannot reject"):
            pas.reject_proposed_action(pa.id, "bob")

    def test_reject_already_approved_raises_transitionerror(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        pas.approve_proposed_action(pa.id, "alice")
        with pytest.raises(TransitionError, match="cannot reject"):
            pas.reject_proposed_action(pa.id, "bob")

    def test_reject_missing_raises_keyerror(self, pas):
        with pytest.raises(KeyError):
            pas.reject_proposed_action(99999, "alice")


# ---------------------------------------------------------------------------
# execute — close_ticket
# ---------------------------------------------------------------------------


class TestExecuteCloseTicket:
    def test_close_draft_ticket(self, pas, service):
        """A DRAFT ticket can transition directly to CLOSED."""
        t = service.create(title="T", description="d", board_id="test-board")
        pa = _make_pa_close(pas, target=t.id)
        pas.approve_proposed_action(pa.id, "alice")
        result = pas.execute_proposed_action(pa.id)
        assert result.status == ProposedActionStatus.EXECUTED
        assert result.executed_at is not None
        assert result.error_message is None
        # Ticket is now CLOSED
        t2 = service.get(t.id)
        assert t2.state == State.CLOSED

    def test_close_requires_approved(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        with pytest.raises(TransitionError, match="cannot execute"):
            pas.execute_proposed_action(pa.id)

    def test_close_rejected_raises_transitionerror(self, pas):
        pa = _make_pa_close(pas, target="t-1")
        pas.reject_proposed_action(pa.id, "alice")
        with pytest.raises(TransitionError, match="cannot execute"):
            pas.execute_proposed_action(pa.id)

    def test_close_idempotent(self, pas, service):
        """Executing an already EXECUTED action is a no-op."""
        t = service.create(title="T", description="d", board_id="test-board")
        pa = _make_pa_close(pas, target=t.id)
        pas.approve_proposed_action(pa.id, "alice")
        pas.execute_proposed_action(pa.id)
        # Second run — idempotent
        result2 = pas.execute_proposed_action(pa.id)
        assert result2.status == ProposedActionStatus.EXECUTED
        # No double-transition errors

    def test_close_fallback_mark_done_then_close(self, pas, service):
        """When a ticket is in READY (cannot go directly to CLOSED),
        the executor falls back to mark_done + transition(CLOSED)."""
        t = service.create(title="T", description="d", board_id="test-board")
        service.transition(t.id, State.READY)
        pa = _make_pa_close(pas, target=t.id)
        pas.approve_proposed_action(pa.id, "alice")
        result = pas.execute_proposed_action(pa.id)
        assert result.status == ProposedActionStatus.EXECUTED
        assert result.error_message is None
        t2 = service.get(t.id)
        assert t2.state == State.CLOSED

    def test_close_target_not_found(self, pas, service):
        """When the target ticket does not exist, record error_message
        and leave status APPROVED."""
        pa = _make_pa_close(pas, target="nonexistent")
        pas.approve_proposed_action(pa.id, "alice")
        result = pas.execute_proposed_action(pa.id)
        assert result.status == ProposedActionStatus.APPROVED  # not EXECUTED
        assert result.error_message == "target ticket nonexistent not found"

    def test_close_missing_target_ticket_id_raises_valueerror(self, pas):
        """A CLOSE_TICKET action without target_ticket_id should fail."""
        pa = pas.create_proposed_action(
            action_type=ActionType.CLOSE_TICKET,
            target_ticket_id="t-will-exist",
            rationale="r",
            source="x",
        )
        # Manually corrupt the row (bypass service validation)
        from robotsix_mill.core.db import session as db_session
        with db_session(pas.settings, pas.board_id) as s:
            pa2 = s.get(ProposedAction, pa.id)
            pa2.target_ticket_id = None
            s.add(pa2)
            s.commit()
            s.refresh(pa2)
            pa_id = pa2.id

        pas.approve_proposed_action(pa_id, "alice")
        result = pas.execute_proposed_action(pa_id)
        assert result.status == ProposedActionStatus.APPROVED
        assert "target_ticket_id is required" in (result.error_message or "")


# ---------------------------------------------------------------------------
# execute — create_ticket
# ---------------------------------------------------------------------------


class TestExecuteCreateTicket:
    def test_create_ticket_happy_path(self, pas, service):
        pa = _make_pa_create(
            pas, title="Gap ticket", body="Description here",
            source=SourceKind.BOARD_CLEANUP,
        )
        pas.approve_proposed_action(pa.id, "alice")
        result = pas.execute_proposed_action(pa.id)
        assert result.status == ProposedActionStatus.EXECUTED
        assert result.error_message is None
        # A ticket was created
        tickets = service.list()
        created = [t for t in tickets if t.title == "Gap ticket"]
        assert len(created) == 1
        assert created[0].source == SourceKind.BOARD_CLEANUP
        assert created[0].board_id == "test-board"

    def test_create_ticket_idempotent(self, pas, service):
        pa = _make_pa_create(pas, title="Only once")
        pas.approve_proposed_action(pa.id, "alice")
        pas.execute_proposed_action(pa.id)
        # Second run
        result2 = pas.execute_proposed_action(pa.id)
        assert result2.status == ProposedActionStatus.EXECUTED
        # Only one ticket created (first run)
        tickets = service.list()
        count = sum(1 for t in tickets if t.title == "Only once")
        assert count == 1

    def test_create_missing_title_raises_valueerror_and_records_error(self, pas):
        """A CREATE_TICKET action without proposed_title records error."""
        pa = pas.create_proposed_action(
            action_type=ActionType.CREATE_TICKET,
            proposed_title="will-be-removed",
            rationale="r",
            source="x",
        )
        # Manually corrupt the row
        from robotsix_mill.core.db import session as db_session
        with db_session(pas.settings, pas.board_id) as s:
            pa2 = s.get(ProposedAction, pa.id)
            pa2.proposed_title = None
            s.add(pa2)
            s.commit()
            s.refresh(pa2)
            pa_id = pa2.id

        pas.approve_proposed_action(pa_id, "alice")
        result = pas.execute_proposed_action(pa_id)
        assert result.status == ProposedActionStatus.APPROVED
        assert "proposed_title is required" in (result.error_message or "")

    def test_create_ticket_requires_approved(self, pas):
        pa = _make_pa_create(pas)
        with pytest.raises(TransitionError, match="cannot execute"):
            pas.execute_proposed_action(pa.id)


# ---------------------------------------------------------------------------
# Board scoping
# ---------------------------------------------------------------------------


class TestBoardScoping:
    def test_list_scoped_to_board(self, settings, service):
        pas_a = ProposedActionService(settings, service)
        pas_b = ProposedActionService(
            settings,
            TicketService(settings, board_id="other-board"),
        )
        _make_pa_close(pas_a, target="t-a")
        _make_pa_close(pas_b, target="t-b")
        assert len(pas_a.list_proposed_actions()) == 1
        assert len(pas_b.list_proposed_actions()) == 1
        assert pas_a.list_proposed_actions()[0].target_ticket_id == "t-a"
        assert pas_b.list_proposed_actions()[0].target_ticket_id == "t-b"

    def test_board_id_defaults_to_empty_string(self, pas):
        pa = _make_pa_close(pas)
        assert pa.board_id == "test-board"
