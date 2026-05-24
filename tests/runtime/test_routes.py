"""Dedicated route-handler tests for endpoints that lacked coverage.

The main ``tests/runtime/test_api.py`` (1479 lines) covers 20 endpoints.
This file fills the remaining gaps with focused, handler-level tests
that exercise the HTTP contract: status codes, response shapes,
parameter clamping, and fire-and-forget behaviour.

All tests use the standard ``client`` + ``service`` fixtures from
``tests/conftest.py``.  No real HTTP call ever escapes — that is
enforced globally by the ``_no_real_http`` autouse fixture.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from fastapi.testclient import TestClient

from robotsix_mill.core.states import State
from robotsix_mill.runtime.api import create_app
from robotsix_mill.runtime.deps import get_worker  # for dependency override


@pytest.fixture
def client(settings):
    """TestClient fixture — identical to the one in test_api.py."""
    with TestClient(create_app(settings)) as c:
        yield c


# ═══════════════════════════════════════════════════════════════════════
# GET /tickets/{id}/history
# ═══════════════════════════════════════════════════════════════════════


def test_history_happy_path(client, service):
    """GET /tickets/{id}/history returns a list of TicketEvent entries
    (created + transition) in chronological order."""
    t = service.create("History test")
    service.transition(t.id, State.READY, note="approved for implement")

    r = client.get(f"/tickets/{t.id}/history")
    assert r.status_code == 200
    events = r.json()
    assert isinstance(events, list)
    assert len(events) >= 2, (
        f"expected ≥2 events (created + transition), got {len(events)}"
    )
    for evt in events:
        assert "id" in evt
        assert evt["ticket_id"] == t.id
        assert "state" in evt
        assert "note" in evt
        assert "at" in evt

    # Chronological order: created first, then the transition.
    assert events[0]["state"] == State.DRAFT
    assert events[1]["state"] == State.READY
    assert events[1]["note"] == "approved for implement"


def test_history_404_nonexistent(client):
    """GET /tickets/nonexistent/history returns 404."""
    r = client.get("/tickets/nonexistent/history")
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# GET /tickets/{id}/comments
# ═══════════════════════════════════════════════════════════════════════


def test_list_comments_happy_path(client, service):
    """GET /tickets/{id}/comments returns all comments for a ticket,
    ordered oldest-first."""
    t = service.create("Comments test")
    service.add_comment(t.id, "first note", author="alice")
    service.add_comment(t.id, "second note", author="bob")

    r = client.get(f"/tickets/{t.id}/comments")
    assert r.status_code == 200
    comments = r.json()
    assert isinstance(comments, list)
    assert len(comments) == 2

    for c in comments:
        assert "id" in c
        assert c["ticket_id"] == t.id
        assert "body" in c
        assert "author" in c
        assert "created_at" in c

    assert comments[0]["body"] == "first note"
    assert comments[0]["author"] == "alice"
    assert comments[1]["body"] == "second note"
    assert comments[1]["author"] == "bob"


def test_list_comments_empty(client, service):
    """GET /tickets/{id}/comments on a ticket with no comments returns [].

    Regression: the endpoint must not 500 when the comment list
    is empty — it must return a valid (empty) JSON array."""
    t = service.create("No comments yet")

    r = client.get(f"/tickets/{t.id}/comments")
    assert r.status_code == 200
    assert r.json() == []


def test_list_comments_404_nonexistent(client):
    """GET /tickets/nonexistent/comments returns 404."""
    r = client.get("/tickets/nonexistent/comments")
    assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# GET /active
# ═══════════════════════════════════════════════════════════════════════


def test_active_empty(client):
    """GET /active returns [] when the worker has no in-flight items."""
    r = client.get("/active")
    assert r.status_code == 200
    assert r.json() == []


def test_active_with_items(client):
    """GET /active returns in-flight tickets when the worker is
    processing items."""
    mock_worker = MagicMock()
    mock_worker._active = {
        "ticket-abc": {
            "stage": "implement",
            "started_at": "2025-01-15T10:00:00Z",
        },
        "ticket-xyz": {
            "stage": "retrospect",
            "started_at": "2025-01-15T10:30:00Z",
        },
    }

    client.app.dependency_overrides[get_worker] = lambda: mock_worker

    r = client.get("/active")
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list)
    assert len(items) == 2
    ids = {item["ticket_id"] for item in items}
    assert ids == {"ticket-abc", "ticket-xyz"}
    for item in items:
        assert "stage" in item
        assert "started_at" in item

    # Cleanup.
    client.app.dependency_overrides.pop(get_worker, None)


# ═══════════════════════════════════════════════════════════════════════
# GET /costs/by-agent
# ═══════════════════════════════════════════════════════════════════════


def test_cost_by_agent_happy_path(client, monkeypatch):
    """GET /costs/by-agent returns aggregated cost data from Langfuse."""
    fake_data = [
        {"name": "refine", "count": 5, "totalCost": 0.12},
        {"name": "implement", "count": 3, "totalCost": 0.45},
    ]

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_by_name",
        lambda settings, lookback_hours: fake_data,
    )

    r = client.get("/costs/by-agent")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    for entry in data:
        assert "name" in entry
        assert "count" in entry
        assert "totalCost" in entry


def test_cost_by_agent_clamps_lookback_low(client, monkeypatch):
    """GET /costs/by-agent?lookback_hours=0 clamps to 1.0."""
    received: list[float] = []

    def capture(settings, lookback_hours):
        received.append(lookback_hours)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_by_name", capture,
    )

    r = client.get("/costs/by-agent?lookback_hours=0")
    assert r.status_code == 200
    assert len(received) == 1
    assert received[0] == 1.0, f"expected 1.0 (clamped from 0), got {received[0]}"


def test_cost_by_agent_clamps_lookback_high(client, monkeypatch):
    """GET /costs/by-agent?lookback_hours=200 clamps to 168.0."""
    received: list[float] = []

    def capture(settings, lookback_hours):
        received.append(lookback_hours)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_by_name", capture,
    )

    r = client.get("/costs/by-agent?lookback_hours=200")
    assert r.status_code == 200
    assert len(received) == 1
    assert received[0] == 168.0, f"expected 168.0 (clamped from 200), got {received[0]}"


# ═══════════════════════════════════════════════════════════════════════
# GET /traces/recent
# ═══════════════════════════════════════════════════════════════════════


def test_traces_recent_happy_path(client, monkeypatch):
    """GET /traces/recent returns serialised trace dicts with expected keys."""
    fake_traces = [
        {"id": "t1", "name": "refine", "timestamp": "2025-01-15T10:00:00Z",
         "sessionId": "sess-1", "totalCost": 0.01, "userId": "u1",
         "extra": "ignored"},  # extra keys should be filtered
        {"id": "t2", "name": "implement", "timestamp": "2025-01-15T11:00:00Z",
         "sessionId": None, "totalCost": 0.05, "userId": None},
    ]

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.list_recent_traces",
        lambda settings, limit, min_cost=None, max_cost=None: fake_traces,
    )

    r = client.get("/traces/recent")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2

    for t in data:
        assert "id" in t
        assert "name" in t
        assert "timestamp" in t
        assert "sessionId" in t
        assert "totalCost" in t
        # Extra keys from the raw trace should be stripped.
        assert "extra" not in t


def test_traces_recent_clamps_limit_low(client, monkeypatch):
    """GET /traces/recent?limit=0 clamps limit to 1."""
    received: list[int] = []

    def capture(settings, limit, min_cost=None, max_cost=None):
        received.append(limit)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.list_recent_traces", capture,
    )

    r = client.get("/traces/recent?limit=0")
    assert r.status_code == 200
    assert len(received) == 1
    assert received[0] == 1, f"expected 1 (clamped from 0), got {received[0]}"


def test_traces_recent_clamps_limit_high(client, monkeypatch):
    """GET /traces/recent?limit=200 clamps limit to 50."""
    received: list[int] = []

    def capture(settings, limit, min_cost=None, max_cost=None):
        received.append(limit)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.list_recent_traces", capture,
    )

    r = client.get("/traces/recent?limit=200")
    assert r.status_code == 200
    assert len(received) == 1
    assert received[0] == 50, f"expected 50 (clamped from 200), got {received[0]}"


# ═══════════════════════════════════════════════════════════════════════
# POST /survey
# ═══════════════════════════════════════════════════════════════════════


def test_survey_fire_and_forget(client, monkeypatch):
    """POST /survey returns 202 immediately and runs the survey in a
    background daemon thread — the HTTP response must not block on the
    LLM call.  Mirrors the test_audit_endpoint_is_fire_and_forget pattern."""
    from robotsix_mill import survey_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []
        updated_memory = ""
        session_id = ""

    def slow_survey():
        ran.set()
        release.wait(5)  # simulate a minutes-long run
        return _R()

    monkeypatch.setattr(survey_runner, "run_survey_pass", slow_survey)

    r = client.post("/survey")  # must NOT block on slow_survey
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5), "survey did not start in background"

    # Clean up: let the daemon thread finish so it doesn't linger
    # after the test.
    release.set()


# ═══════════════════════════════════════════════════════════════════════
# POST /tickets/{id}/transition — happy path
# ═══════════════════════════════════════════════════════════════════════


def test_transition_happy_path_draft_to_ready(client, service):
    """POST /tickets/{id}/transition with {"state":"ready"} transitions
    a DRAFT ticket to READY and returns the updated TicketRead.

    The existing transition tests in test_api.py only cover error
    cases (409 on illegal transitions) and the blocked-override
    paths.  This covers the simple-success case.
    """
    t = service.create("Transition happy-path test")

    r = client.post(
        f"/tickets/{t.id}/transition",
        json={"state": "ready", "note": "operator-approved"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id
    assert data["title"] == "Transition happy-path test"
    assert data["state"] == "ready"
    assert "cost_usd" in data

    # The worker races to process the ticket (implement stage picks up
    # READY tickets).  Accept-transition responded successfully; the
    # worker may have already moved the ticket out of READY by the
    # time we check, so we assert the response, not the final DB state.
