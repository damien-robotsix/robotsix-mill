"""Tests for ``Service.execute_proposed_action`` — the deterministic
dispatch that applies approved ``ProposedAction`` rows to their target
tickets."""

import pytest

from robotsix_mill.core.models import (
    ProposedAction,
    ActionType,
    ProposedActionStatus,
)
from robotsix_mill.core.states import State


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _create_proposed(
    service,
    *,
    action_type: ActionType,
    target_ticket_id: str,
    source: str = "test-agent",
    rationale: str = "test rationale",
    payload: str | None = None,
) -> ProposedAction:
    """Insert a PENDING ProposedAction directly into the DB."""
    from robotsix_mill.core import db
    from datetime import datetime, timezone

    pa = ProposedAction(
        source=source,
        target_ticket_id=target_ticket_id,
        action_type=action_type,
        payload=payload,
        rationale=rationale,
        status=ProposedActionStatus.APPROVED,
        created_at=datetime.now(timezone.utc),
    )
    with db.session(service.settings, service.board_id) as s:
        s.add(pa)
        s.commit()
        s.refresh(pa)
        return pa


def _ticket_state(service, ticket_id: str) -> State:
    return service.get(ticket_id).state


def _history_notes(service, ticket_id: str) -> list[str]:
    return [e.note or "" for e in service.history(ticket_id)]


def _comments(service, ticket_id: str) -> list[str]:
    return [c.body for c in service.list_comments(ticket_id)]


# ---------------------------------------------------------------------------
# execute_proposed_action — basic gate tests
# ---------------------------------------------------------------------------


def test_requires_board_id(service):
    """execute_proposed_action raises ValueError when board_id is empty."""
    from robotsix_mill.core.service import TicketService

    svc = TicketService(service.settings, board_id="")
    with pytest.raises(ValueError, match="requires a board_id"):
        svc.execute_proposed_action(1, decided_by="tester")


def test_missing_action_raises_keyerror(service):
    with pytest.raises(KeyError):
        service.execute_proposed_action(9999, decided_by="tester")


def test_pending_action_is_noop(service):
    """PENDING rows are never executed — returned unchanged."""
    from robotsix_mill.core import db
    from datetime import datetime, timezone

    pa = ProposedAction(
        source="health",
        target_ticket_id="nonexistent-ticket",
        action_type=ActionType.CLOSE,
        rationale="should not execute",
        status=ProposedActionStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    with db.session(service.settings, service.board_id) as s:
        s.add(pa)
        s.commit()
        s.refresh(pa)
        action_id = pa.id

    result = service.execute_proposed_action(action_id, decided_by="tester")
    assert result.status == ProposedActionStatus.PENDING
    assert result.decided_at is None


def test_rejected_action_is_noop(service):
    """REJECTED rows are never executed — returned unchanged."""
    from robotsix_mill.core import db
    from datetime import datetime, timezone

    pa = ProposedAction(
        source="health",
        target_ticket_id="nonexistent-ticket",
        action_type=ActionType.CLOSE,
        rationale="should not execute",
        status=ProposedActionStatus.REJECTED,
        created_at=datetime.now(timezone.utc),
    )
    with db.session(service.settings, service.board_id) as s:
        s.add(pa)
        s.commit()
        s.refresh(pa)
        action_id = pa.id

    result = service.execute_proposed_action(action_id, decided_by="tester")
    assert result.status == ProposedActionStatus.REJECTED


# ---------------------------------------------------------------------------
# CLOSE
# ---------------------------------------------------------------------------


def test_close_happy_path(service):
    """CLOSE transitions DONE → CLOSED and records a TicketEvent note."""
    t = service.create("close-test")
    # Walk to DONE
    service.transition(t.id, State.DONE)

    pa = _create_proposed(
        service,
        action_type=ActionType.CLOSE,
        target_ticket_id=t.id,
        rationale="stale ticket with no activity for 90 days",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.EXECUTED
    assert result.failure_reason is None
    assert result.decided_by == "operator"
    assert result.decided_at is not None

    assert _ticket_state(service, t.id) == State.CLOSED
    notes = _history_notes(service, t.id)
    assert any("closed via proposed action" in n for n in notes)


def test_close_on_non_done_fails(service):
    """CLOSE on a ticket in a state that cannot reach CLOSED (e.g. READY)
    raises TransitionError → FAILED."""
    t = service.create("not-done-yet")
    service.transition(t.id, State.READY)  # READY → CLOSED is illegal

    pa = _create_proposed(
        service,
        action_type=ActionType.CLOSE,
        target_ticket_id=t.id,
        rationale="trying to close early",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert result.failure_reason is not None
    assert "not allowed" in result.failure_reason


def test_close_missing_ticket_fails(service):
    """CLOSE on a nonexistent ticket raises KeyError → FAILED."""
    pa = _create_proposed(
        service,
        action_type=ActionType.CLOSE,
        target_ticket_id="does-not-exist",
        rationale="ghost ticket",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert result.failure_reason is not None
    assert "does-not-exist" in result.failure_reason


# ---------------------------------------------------------------------------
# TRANSITION
# ---------------------------------------------------------------------------


def test_transition_happy_path(service):
    """TRANSITION with valid payload moves the ticket."""
    t = service.create("transition-test")
    service.transition(t.id, State.READY)
    assert _ticket_state(service, t.id) == State.READY

    pa = _create_proposed(
        service,
        action_type=ActionType.TRANSITION,
        target_ticket_id=t.id,
        payload='{"state": "deliverable"}',
        rationale="ready for delivery",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.EXECUTED
    assert result.failure_reason is None

    assert _ticket_state(service, t.id) == State.DELIVERABLE
    notes = _history_notes(service, t.id)
    assert any("transitioned to deliverable via proposed action" in n for n in notes)


def test_transition_invalid_state_fails(service):
    """TRANSITION to an invalid state string raises ValueError → FAILED."""
    t = service.create("bad-transition")

    pa = _create_proposed(
        service,
        action_type=ActionType.TRANSITION,
        target_ticket_id=t.id,
        payload='{"state": "nonexistent-state"}',
        rationale="invalid",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert result.failure_reason is not None


def test_transition_malformed_json_fails(service):
    """TRANSITION with unparseable JSON → FAILED."""
    t = service.create("malformed-json")

    pa = _create_proposed(
        service,
        action_type=ActionType.TRANSITION,
        target_ticket_id=t.id,
        payload="not-json",
        rationale="bad payload",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert result.failure_reason is not None


def test_transition_empty_payload_uses_default(service):
    """Empty payload '{}' has no 'state' key → KeyError on data['state']."""
    t = service.create("empty-payload")

    pa = _create_proposed(
        service,
        action_type=ActionType.TRANSITION,
        target_ticket_id=t.id,
        payload="{}",
        rationale="missing state",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert "state" in (result.failure_reason or "")


def test_transition_illegal_transition_fails(service):
    """TRANSITION from ANSWERED → READY is illegal → FAILED."""
    t = service.create("illegal-transition", kind="inquiry")
    service.transition(t.id, State.ANSWERED)  # terminal state

    pa = _create_proposed(
        service,
        action_type=ActionType.TRANSITION,
        target_ticket_id=t.id,
        payload='{"state": "ready"}',
        rationale="cannot move answered inquiry to ready",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert "not allowed" in result.failure_reason.lower()


# ---------------------------------------------------------------------------
# COMMENT
# ---------------------------------------------------------------------------


def test_comment_happy_path(service):
    """COMMENT posts a comment and history breadcrumb."""
    t = service.create("comment-test")

    pa = _create_proposed(
        service,
        action_type=ActionType.COMMENT,
        target_ticket_id=t.id,
        rationale="missing test coverage noted",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.EXECUTED

    # Comment exists.
    comments = _comments(service, t.id)
    assert any("missing test coverage" in c for c in comments)

    # History breadcrumb exists.
    notes = _history_notes(service, t.id)
    assert any("comment added via proposed action" in n for n in notes)


def test_comment_missing_ticket_fails(service):
    """COMMENT on nonexistent ticket → FAILED."""
    pa = _create_proposed(
        service,
        action_type=ActionType.COMMENT,
        target_ticket_id="does-not-exist",
        rationale="ghost comment",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert "does-not-exist" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# RELABEL
# ---------------------------------------------------------------------------


def _labels(service, ticket_id):
    from robotsix_mill.core.service import _parse_labels

    return _parse_labels(service.get(ticket_id).labels)


def test_relabel_add_happy_path(service):
    """RELABEL add on a label-less ticket applies the labels and notes."""
    t = service.create("relabel-add")

    pa = _create_proposed(
        service,
        action_type=ActionType.RELABEL,
        target_ticket_id=t.id,
        payload='{"add": ["bug", "urgent"]}',
        rationale="triage outcome",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.EXECUTED
    assert result.failure_reason is None
    assert _labels(service, t.id) == ["bug", "urgent"]

    notes = _history_notes(service, t.id)
    assert any("relabeled via proposed action" in n for n in notes)


def test_relabel_remove(service):
    """RELABEL remove drops the named label."""
    t = service.create("relabel-remove")
    service.set_labels(t.id, ["bug", "stale"])

    pa = _create_proposed(
        service,
        action_type=ActionType.RELABEL,
        target_ticket_id=t.id,
        payload='{"remove": ["stale"]}',
        rationale="not stale anymore",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.EXECUTED
    assert _labels(service, t.id) == ["bug"]


def test_relabel_set_replaces(service):
    """RELABEL set replaces the label list entirely."""
    t = service.create("relabel-set")
    service.set_labels(t.id, ["bug", "urgent"])

    pa = _create_proposed(
        service,
        action_type=ActionType.RELABEL,
        target_ticket_id=t.id,
        payload='{"set": ["only"]}',
        rationale="reset labels",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.EXECUTED
    assert _labels(service, t.id) == ["only"]


def test_relabel_dedupes(service):
    """RELABEL add dedupes repeated labels preserving order."""
    t = service.create("relabel-dedupe")

    pa = _create_proposed(
        service,
        action_type=ActionType.RELABEL,
        target_ticket_id=t.id,
        payload='{"add": ["a", "a"]}',
        rationale="dupes",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.EXECUTED
    assert _labels(service, t.id) == ["a"]


def test_relabel_empty_payload_fails(service):
    """RELABEL with an empty payload → FAILED naming the required keys."""
    t = service.create("relabel-empty")

    pa = _create_proposed(
        service,
        action_type=ActionType.RELABEL,
        target_ticket_id=t.id,
        payload="{}",
        rationale="nothing to do",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert "set" in (result.failure_reason or "")
    assert "add" in (result.failure_reason or "")
    assert "remove" in (result.failure_reason or "")


def test_relabel_missing_ticket_fails(service):
    """RELABEL on a nonexistent ticket → FAILED naming the id."""
    pa = _create_proposed(
        service,
        action_type=ActionType.RELABEL,
        target_ticket_id="does-not-exist",
        payload='{"add": ["bug"]}',
        rationale="ghost relabel",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert "does-not-exist" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# unknown action type guard
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# unknown action type guard
#
# The dispatch table includes an ``else: raise ValueError("unknown action
# type")`` branch.  SQLAlchemy's Enum type rejects values not in
# ``ActionType`` at load time (raising ``LookupError``), so this branch
# is unreachable through normal DB operations.  It remains as defence-
# in-depth against future changes (e.g. switching to a plain string
# column).  No test exercises the unreachable branch.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_idempotent_executed_is_noop(service):
    """Calling execute on an already-EXECUTED row returns it unchanged."""
    t = service.create("idempotent-test")
    service.transition(t.id, State.DONE)

    pa = _create_proposed(
        service,
        action_type=ActionType.CLOSE,
        target_ticket_id=t.id,
        rationale="first close",
    )
    r1 = service.execute_proposed_action(pa.id, decided_by="operator")
    assert r1.status == ProposedActionStatus.EXECUTED

    # Second call — no-op.
    r2 = service.execute_proposed_action(pa.id, decided_by="operator")
    assert r2.status == ProposedActionStatus.EXECUTED
    assert r2.decided_by == "operator"
    # The ticket was already CLOSED — no second event for CLOSE.
    # Verify that only one CLOSED event exists (from the executor + the
    # original DONE transition).
    close_events = [e for e in service.history(t.id) if e.state == State.CLOSED]
    assert len(close_events) == 1


def test_idempotent_failed_is_noop(service):
    """Calling execute on an already-FAILED row returns it unchanged."""
    t = service.create("failed-idempotent")

    pa = _create_proposed(
        service,
        action_type=ActionType.RELABEL,
        target_ticket_id=t.id,
        rationale="will fail because label infra missing",
    )
    r1 = service.execute_proposed_action(pa.id, decided_by="operator")
    assert r1.status == ProposedActionStatus.FAILED

    r2 = service.execute_proposed_action(pa.id, decided_by="operator")
    assert r2.status == ProposedActionStatus.FAILED
    assert r2.decided_by == "operator"


# ---------------------------------------------------------------------------
# _action_note format
# ---------------------------------------------------------------------------


def test_action_note_format():
    from robotsix_mill.core.service import TicketService

    note = TicketService._action_note(
        "closed", "health", "stale ticket with no activity for 90 days"
    )
    assert note == (
        "[health] closed via proposed action: stale ticket with no activity for 90 days"
    )


def test_action_note_in_ticket_event(service):
    """Verify the full note appears in a TicketEvent after CLOSE."""
    t = service.create("note-format-test")
    service.transition(t.id, State.DONE)

    pa = _create_proposed(
        service,
        action_type=ActionType.CLOSE,
        target_ticket_id=t.id,
        source="trace-review",
        rationale="false positive resolved",
    )
    service.execute_proposed_action(pa.id, decided_by="operator")

    events = service.history(t.id)
    close_event = events[-1]
    assert close_event.note == (
        "[trace-review] closed via proposed action: false positive resolved"
    )


# ---------------------------------------------------------------------------
# TRANSITION payload = None
# ---------------------------------------------------------------------------


def test_transition_payload_none_uses_empty_dict(service):
    """TRANSITION with payload=None → treated as '{}' → KeyError on 'state'."""
    t = service.create("none-payload")

    pa = _create_proposed(
        service,
        action_type=ActionType.TRANSITION,
        target_ticket_id=t.id,
        payload=None,
        rationale="missing state key",
    )
    result = service.execute_proposed_action(pa.id, decided_by="operator")
    assert result.status == ProposedActionStatus.FAILED
    assert "state" in (result.failure_reason or "")


# ---------------------------------------------------------------------------
# close_thread integration: ensure close_thread doesn't interfere
# ---------------------------------------------------------------------------


def test_close_thread_does_not_interfere_with_executor(service):
    """A close done via proposed action doesn't break close_thread."""
    t = service.create("dual-close-test")
    service.transition(t.id, State.DONE)

    pa = _create_proposed(
        service,
        action_type=ActionType.CLOSE,
        target_ticket_id=t.id,
        rationale="agent closed",
    )
    service.execute_proposed_action(pa.id, decided_by="operator")
    assert _ticket_state(service, t.id) == State.CLOSED

    # close_thread still works after the executor closed the ticket.
    c = service.add_comment(t.id, "thread comment", author="reviewer")
    closed = service.close_thread(c.id)
    assert closed.closed_at is not None
