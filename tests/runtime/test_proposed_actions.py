"""HTTP-level tests for the proposed-action review routes.

Covers the 4 endpoints (list, get, approve, reject) plus the
idempotent executor behaviour for CLOSE and COMMENT action types.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core import db
from robotsix_mill.core.models import (
    ProposedAction,
    ProposedActionStatus,
    ActionType,
)
from robotsix_mill.core.states import State
from robotsix_mill.runtime.api import create_app


# -- fixtures -----------------------------------------------------------


@pytest.fixture
def client(settings, repos_registry):
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


# -- helpers ------------------------------------------------------------


def _seed_action(settings, **fields) -> ProposedAction:
    """Insert a ProposedAction row into the test DB and return it."""
    defaults = {
        "source": "health",
        "target_ticket_id": "T-nonexistent",
        "action_type": ActionType.CLOSE,
        "payload": None,
        "rationale": "test",
        "status": ProposedActionStatus.PENDING,
    }
    defaults.update(fields)
    with db.session(settings, "test-board") as s:
        action = ProposedAction(**defaults)
        s.add(action)
        s.commit()
        s.refresh(action)
        return action


# -- GET /proposed-actions -----------------------------------------------


def test_list_empty(client):
    """GET /proposed-actions returns empty list when no actions exist."""
    r = client.get("/proposed-actions")
    assert r.status_code == 200
    assert r.json() == []


def test_list_all_sorted_by_created_at_desc(client, service, settings):
    """GET /proposed-actions returns all actions sorted newest-first."""
    t = service.create("Target ticket")
    # Explicit created_at (not a wall-clock sleep) so the DESC ordering is
    # deterministic — the old _sleep_ms(10) could let two rows share a
    # timestamp under load, flaking the assertion and poisoning the gate.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a1 = _seed_action(
        settings,
        source="health",
        target_ticket_id=t.id,
        action_type=ActionType.CLOSE,
        rationale="first",
        created_at=base,
    )
    a2 = _seed_action(
        settings,
        source="survey",
        target_ticket_id=t.id,
        action_type=ActionType.COMMENT,
        payload='{"body":"hello"}',
        rationale="second",
        created_at=base + timedelta(seconds=1),
    )

    r = client.get("/proposed-actions")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    # Sorted by created_at DESC → a2 (newer) first.
    assert data[0]["id"] == a2.id
    assert data[1]["id"] == a1.id
    assert data[0]["source"] == "survey"
    assert data[1]["source"] == "health"


def test_list_filter_by_status(client, service, settings):
    """GET /proposed-actions?status=pending returns only PENDING rows."""
    t = service.create("Target ticket")
    _seed_action(
        settings,
        source="health",
        target_ticket_id=t.id,
        action_type=ActionType.CLOSE,
        status=ProposedActionStatus.PENDING,
        rationale="pending",
    )
    _seed_action(
        settings,
        source="survey",
        target_ticket_id=t.id,
        action_type=ActionType.COMMENT,
        payload='{"body":"x"}',
        status=ProposedActionStatus.REJECTED,
        rationale="rejected",
    )

    r = client.get("/proposed-actions", params={"status": "pending"})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["status"] == "pending"
    assert data[0]["source"] == "health"


# -- GET /proposed-actions/{id} -----------------------------------------


def test_get_by_id_happy_path(client, service, settings):
    """GET /proposed-actions/{id} returns the action for a valid id."""
    t = service.create("Target ticket")
    a = _seed_action(
        settings,
        source="health",
        target_ticket_id=t.id,
        action_type=ActionType.CLOSE,
        rationale="test get",
    )

    r = client.get(f"/proposed-actions/{a.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == a.id
    assert data["source"] == "health"
    assert data["target_ticket_id"] == t.id
    assert data["status"] == "pending"


def test_get_by_id_404(client):
    """GET /proposed-actions/{nonexistent} returns 404."""
    r = client.get("/proposed-actions/99999")
    assert r.status_code == 404


# -- POST /proposed-actions/{id}/approve --------------------------------


def test_approve_happy_path(client, service, settings):
    """POST /proposed-actions/{id}/approve transitions PENDING →
    EXECUTED and returns the updated action."""
    t = service.create("Approve target")
    # Transition to DONE so CLOSE is a valid transition.
    service.transition(t.id, State.DONE)
    a = _seed_action(
        settings,
        source="health",
        target_ticket_id=t.id,
        action_type=ActionType.CLOSE,
        rationale="cleanup",
    )

    r = client.post(f"/proposed-actions/{a.id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == a.id
    assert data["status"] == "executed"
    assert data["decided_at"] is not None
    assert data["decided_by"] == "human"


def test_approve_already_executed_returns_400(client, service, settings):
    """POST /proposed-actions/{id}/approve on already-EXECUTED action
    returns 400 (not PENDING)."""
    t = service.create("Already exec target")
    service.transition(t.id, State.DONE)
    a = _seed_action(
        settings,
        source="health",
        target_ticket_id=t.id,
        action_type=ActionType.CLOSE,
        rationale="first",
    )

    # First approve succeeds.
    r1 = client.post(f"/proposed-actions/{a.id}/approve")
    assert r1.status_code == 200
    assert r1.json()["status"] == "executed"

    # Second approve → 400.
    r2 = client.post(f"/proposed-actions/{a.id}/approve")
    assert r2.status_code == 400


# -- POST /proposed-actions/{id}/reject ---------------------------------


def test_reject_happy_path(client, service, settings):
    """POST /proposed-actions/{id}/reject transitions PENDING →
    REJECTED."""
    t = service.create("Reject target")
    a = _seed_action(
        settings,
        source="survey",
        target_ticket_id=t.id,
        action_type=ActionType.COMMENT,
        payload='{"body":"nope"}',
        rationale="should reject",
    )

    r = client.post(f"/proposed-actions/{a.id}/reject")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == a.id
    assert data["status"] == "rejected"
    assert data["decided_at"] is not None
    assert data["decided_by"] == "human"


def test_reject_already_rejected_returns_400(client, service, settings):
    """POST /proposed-actions/{id}/reject on already-REJECTED action
    returns 400."""
    t = service.create("Double reject target")
    a = _seed_action(
        settings,
        source="survey",
        target_ticket_id=t.id,
        action_type=ActionType.COMMENT,
        payload='{"body":"nope"}',
        rationale="reject once",
    )

    # First reject succeeds.
    r1 = client.post(f"/proposed-actions/{a.id}/reject")
    assert r1.status_code == 200
    assert r1.json()["status"] == "rejected"

    # Second reject → 400.
    r2 = client.post(f"/proposed-actions/{a.id}/reject")
    assert r2.status_code == 400


# -- Executor: CLOSE action type ----------------------------------------


def test_approve_executes_close(client, service, settings):
    """Approve a CLOSE action → ticket transitions to CLOSED."""
    t = service.create("Close me")
    # Must be in a state that allows → CLOSED: DONE works.
    service.transition(t.id, State.DONE)
    a = _seed_action(
        settings,
        source="health",
        target_ticket_id=t.id,
        action_type=ActionType.CLOSE,
        rationale="time to close",
    )

    r = client.post(f"/proposed-actions/{a.id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "executed"

    # Verify ticket is now CLOSED.
    ticket = service.get(t.id)
    assert ticket.state == State.CLOSED


# -- Executor: COMMENT action type --------------------------------------


def test_approve_executes_comment(client, service, settings):
    """Approve a COMMENT action → comment is added to the target ticket."""
    t = service.create("Comment target")
    a = _seed_action(
        settings,
        source="survey",
        target_ticket_id=t.id,
        action_type=ActionType.COMMENT,
        payload='{"body":"auto-comment from periodic agent"}',
        rationale="add note",
    )

    r = client.post(f"/proposed-actions/{a.id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "executed"

    # Verify comment was added — the consolidated executor posts the
    # action's rationale as the body, authored by the action source,
    # and writes a history breadcrumb note.
    comments = service.list_comments(t.id)
    assert len(comments) == 1
    assert comments[0].body == "add note"
    assert comments[0].author == "survey"

    history = service.history(t.id)
    assert any(
        ev.note and "comment added via proposed action" in ev.note for ev in history
    )
