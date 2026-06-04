"""HTTP-level tests for the proposed-action routes.

Covers GET /proposed-actions, POST /proposed-actions/{id}/approve
(all four ActionType values), and POST /proposed-actions/{id}/reject,
plus 404 and 409 edge cases.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core import db
from robotsix_mill.core.models import (
    ActionType,
    ProposedAction,
    ProposedActionStatus,
)
from robotsix_mill.core.states import State
from robotsix_mill.runtime.api import create_app


@pytest.fixture
def client(settings, repos_registry):
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


@pytest.fixture
def ticket(service):
    """Create a ticket so ProposedAction rows have a valid FK target."""
    t = service.create("test ticket", "body", source="user", board_id="test-board")
    return t


@pytest.fixture
def pa_pending_close(settings, ticket):
    """A pending CLOSE proposed action on *ticket*."""
    with db.session(settings, "test-board") as s:
        pa = ProposedAction(
            source="health",
            target_ticket_id=ticket.id,
            action_type=ActionType.CLOSE,
            rationale="No longer needed",
        )
        s.add(pa)
        s.commit()
        s.refresh(pa)
        return pa


@pytest.fixture
def pa_pending_transition(settings, ticket):
    """A pending TRANSITION proposed action (DRAFT → READY)."""
    with db.session(settings, "test-board") as s:
        pa = ProposedAction(
            source="audit",
            target_ticket_id=ticket.id,
            action_type=ActionType.TRANSITION,
            payload=json.dumps({"to_state": "ready"}),
            rationale="Approve and move to ready",
        )
        s.add(pa)
        s.commit()
        s.refresh(pa)
        return pa


@pytest.fixture
def pa_pending_comment(settings, ticket):
    """A pending COMMENT proposed action."""
    with db.session(settings, "test-board") as s:
        pa = ProposedAction(
            source="survey",
            target_ticket_id=ticket.id,
            action_type=ActionType.COMMENT,
            payload="This is a note from the agent.",
            rationale="Agent wants to leave a note",
        )
        s.add(pa)
        s.commit()
        s.refresh(pa)
        return pa


@pytest.fixture
def pa_pending_relabel(settings, ticket):
    """A pending RELABEL proposed action (set priority=True)."""
    with db.session(settings, "test-board") as s:
        pa = ProposedAction(
            source="health",
            target_ticket_id=ticket.id,
            action_type=ActionType.RELABEL,
            payload=json.dumps({"priority": True}),
            rationale="This ticket should be high priority",
        )
        s.add(pa)
        s.commit()
        s.refresh(pa)
        return pa


# -- list ----------------------------------------------------------------


def test_list_proposed_actions_default_pending(
    client, pa_pending_close, pa_pending_transition
):
    r = client.get("/proposed-actions?repo_id=test-repo")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    for pa in data:
        assert pa["status"] == "pending"
    # Sorted by created_at descending — most recent first.
    ids = [pa["id"] for pa in data]
    assert ids == sorted(ids, reverse=True)


def test_list_proposed_actions_status_filter(
    client, settings, ticket, pa_pending_close
):
    # Create a second PA and approve it.
    with db.session(settings, "test-board") as s:
        pa2 = ProposedAction(
            source="audit",
            target_ticket_id=ticket.id,
            action_type=ActionType.COMMENT,
            rationale="Already done",
            status=ProposedActionStatus.APPROVED,
        )
        s.add(pa2)
        s.commit()

    r_all = client.get("/proposed-actions?repo_id=test-repo&status=all")
    assert r_all.status_code == 200
    assert len(r_all.json()) == 2

    r_approved = client.get("/proposed-actions?repo_id=test-repo&status=approved")
    assert r_approved.status_code == 200
    approved = r_approved.json()
    assert len(approved) == 1
    assert approved[0]["status"] == "approved"


def test_list_proposed_actions_unknown_repo_400(client):
    r = client.get("/proposed-actions?repo_id=unknown-repo")
    assert r.status_code == 400


def test_list_proposed_actions_empty(client):
    r = client.get("/proposed-actions?repo_id=test-repo")
    assert r.status_code == 200
    assert r.json() == []


# -- approve (close) -----------------------------------------------------


def test_approve_close(client, pa_pending_close, service):
    ticket_id = pa_pending_close.target_ticket_id

    r = client.post(
        f"/proposed-actions/{pa_pending_close.id}/approve?repo_id=test-repo"
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "executed"
    assert data["decided_by"] == "human"
    assert data["decided_at"] is not None

    # The target ticket is now CLOSED.
    t = service.get(ticket_id)
    assert t is not None
    assert t.state == State.CLOSED


# -- approve (transition) ------------------------------------------------


def test_approve_transition(client, pa_pending_transition, service):
    ticket_id = pa_pending_transition.target_ticket_id

    r = client.post(
        f"/proposed-actions/{pa_pending_transition.id}/approve?repo_id=test-repo"
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "executed"

    # The target ticket is now READY.
    t = service.get(ticket_id)
    assert t is not None
    assert t.state == State.READY


def test_approve_transition_invalid_payload(client, settings, ticket):
    """A TRANSITION with a bogus to_state fails with 500."""
    with db.session(settings, "test-board") as s:
        pa = ProposedAction(
            source="audit",
            target_ticket_id=ticket.id,
            action_type=ActionType.TRANSITION,
            payload=json.dumps({"to_state": "bogus"}),
            rationale="Invalid payload",
        )
        s.add(pa)
        s.commit()
        s.refresh(pa)
        pa_id = pa.id

    r = client.post(f"/proposed-actions/{pa_id}/approve?repo_id=test-repo")
    assert r.status_code == 500
    assert "execution failed" in r.text.lower() or "500" in str(r.status_code)

    # The PA is marked FAILED.
    with db.session(settings, "test-board") as s:
        pa = s.get(ProposedAction, pa_id)
        assert pa is not None
        assert pa.status == ProposedActionStatus.FAILED


# -- approve (comment) ---------------------------------------------------


def test_approve_comment(client, pa_pending_comment, service):
    ticket_id = pa_pending_comment.target_ticket_id

    r = client.post(
        f"/proposed-actions/{pa_pending_comment.id}/approve?repo_id=test-repo"
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "executed"

    # The target ticket has a new comment.
    comments = service.list_comments(ticket_id)
    assert len(comments) == 1
    assert comments[0].body == "This is a note from the agent."
    assert comments[0].author == "proposed-action"


# -- approve (relabel) ---------------------------------------------------


def test_approve_relabel(client, pa_pending_relabel, service, settings):
    ticket_id = pa_pending_relabel.target_ticket_id

    # Priority should start False.
    t_before = service.get(ticket_id)
    assert t_before.priority is False

    r = client.post(
        f"/proposed-actions/{pa_pending_relabel.id}/approve?repo_id=test-repo"
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "executed"

    # Priority is now True.
    t_after = service.get(ticket_id)
    assert t_after.priority is True


def test_approve_relabel_invalid_payload(client, settings, ticket):
    """A RELABEL with missing 'priority' fails with 500."""
    with db.session(settings, "test-board") as s:
        pa = ProposedAction(
            source="health",
            target_ticket_id=ticket.id,
            action_type=ActionType.RELABEL,
            payload=json.dumps({"foo": "bar"}),
            rationale="Invalid payload",
        )
        s.add(pa)
        s.commit()
        s.refresh(pa)
        pa_id = pa.id

    r = client.post(f"/proposed-actions/{pa_id}/approve?repo_id=test-repo")
    assert r.status_code == 500
    assert "execution failed" in r.text.lower() or "500" in str(r.status_code)

    # The PA is marked FAILED.
    with db.session(settings, "test-board") as s:
        pa = s.get(ProposedAction, pa_id)
        assert pa is not None
        assert pa.status == ProposedActionStatus.FAILED


# -- reject --------------------------------------------------------------


def test_reject_pending(client, pa_pending_close, service):
    ticket_id = pa_pending_close.target_ticket_id

    r = client.post(f"/proposed-actions/{pa_pending_close.id}/reject?repo_id=test-repo")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "rejected"
    assert data["decided_by"] == "human"
    assert data["decided_at"] is not None

    # The target ticket is untouched (still DRAFT).
    t = service.get(ticket_id)
    assert t is not None
    assert t.state == State.DRAFT


# -- 404 / 409 -----------------------------------------------------------


def test_approve_missing_404(client):
    r = client.post("/proposed-actions/99999/approve?repo_id=test-repo")
    assert r.status_code == 404


def test_reject_missing_404(client):
    r = client.post("/proposed-actions/99999/reject?repo_id=test-repo")
    assert r.status_code == 404


def test_approve_already_decided_409(client, pa_pending_close):
    # First approve.
    client.post(f"/proposed-actions/{pa_pending_close.id}/approve?repo_id=test-repo")
    # Second approve.
    r = client.post(
        f"/proposed-actions/{pa_pending_close.id}/approve?repo_id=test-repo"
    )
    assert r.status_code == 409


def test_reject_already_decided_409(client, pa_pending_close):
    # First reject.
    client.post(f"/proposed-actions/{pa_pending_close.id}/reject?repo_id=test-repo")
    # Second reject.
    r = client.post(f"/proposed-actions/{pa_pending_close.id}/reject?repo_id=test-repo")
    assert r.status_code == 409


def test_approve_unknown_repo_400(client, pa_pending_close):
    r = client.post(
        f"/proposed-actions/{pa_pending_close.id}/approve?repo_id=unknown-repo"
    )
    assert r.status_code == 400


def test_reject_unknown_repo_400(client, pa_pending_close):
    r = client.post(
        f"/proposed-actions/{pa_pending_close.id}/reject?repo_id=unknown-repo"
    )
    assert r.status_code == 400
