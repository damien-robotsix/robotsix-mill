"""Dedicated tests for previously-untested route handlers.

Covers the 7 endpoint groups that had zero HTTP-level coverage in the
existing 1479-line ``tests/runtime/test_api.py``:

* ``GET /tickets/{id}/history``
* ``GET /tickets/{id}/comments``
* ``GET /active``
* ``GET /costs/by-agent``
* ``GET /traces/recent``
* ``POST /survey``
* ``POST /tickets/{id}/transition`` (happy-path only; error cases are
  already tested elsewhere)
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.states import State
from robotsix_mill.runtime.api import create_app


# -- fixtures -----------------------------------------------------------


@pytest.fixture
def client(settings, repos_registry):
    """Reusable TestClient wired to the same lifespan as test_api.py."""
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


# -- GET /tickets/{id}/history ------------------------------------------


def test_get_history_happy_path(client, service):
    """GET /tickets/{id}/history returns list of events for a ticket
    that has been created and transitioned."""
    t = service.create("History test")
    service.transition(t.id, State.READY, note="promoted to ready")

    r = client.get(f"/tickets/{t.id}/history")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list), f"expected list, got {type(data)}"
    assert len(data) >= 2, (
        f"expected >=2 events (created + transition), got {len(data)}"
    )
    for evt in data:
        for key in ("id", "ticket_id", "state", "note", "at"):
            assert key in evt, f"event missing key '{key}': {evt}"


def test_get_history_404(client):
    """GET /tickets/{nonexistent}/history returns 404."""
    r = client.get("/tickets/nonexistent/history")
    assert r.status_code == 404


# -- GET /tickets/{id}/comments -----------------------------------------


def test_get_comments_happy_path(client, service):
    """GET /tickets/{id}/comments returns all comments for a ticket."""
    t = service.create("Comments test")
    service.add_comment(t.id, "First comment", author="alice")
    service.add_comment(t.id, "Second comment", author="bob")

    r = client.get(f"/tickets/{t.id}/comments")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    for c in data:
        for key in ("id", "ticket_id", "body", "author", "created_at"):
            assert key in c, f"comment missing key '{key}': {c}"
    bodies = {c["body"] for c in data}
    assert bodies == {"First comment", "Second comment"}


def test_get_comments_empty(client, service):
    """GET /tickets/{id}/comments returns empty list when no comments exist."""
    t = service.create("No comments yet")

    r = client.get(f"/tickets/{t.id}/comments")
    assert r.status_code == 200
    assert r.json() == []


def test_get_comments_404(client):
    """GET /tickets/{nonexistent}/comments returns 404."""
    r = client.get("/tickets/nonexistent/comments")
    assert r.status_code == 404


# -- POST /comments/{id}/close and /reopen ------------------------------


def test_close_thread_happy_path(client, service):
    """POST /comments/{id}/close on an open top-level comment returns 200
    with closed_at set."""
    t = service.create("Close test")
    c = service.add_comment(t.id, "Thread to close", author="alice")

    r = client.post(f"/comments/{c.id}/close")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == c.id
    assert data["closed_at"] is not None
    assert data["parent_id"] is None  # top-level


def test_close_thread_on_reply_returns_409(client, service):
    """POST /comments/{id}/close on a reply returns 409."""
    t = service.create("Close reply test")
    parent = service.add_comment(t.id, "Parent thread", author="alice")
    reply = service.add_comment(t.id, "A reply", author="bob", parent_id=parent.id)

    r = client.post(f"/comments/{reply.id}/close")
    assert r.status_code == 409


def test_close_thread_already_closed_returns_409(client, service):
    """POST /comments/{id}/close on an already-closed thread returns 409."""
    t = service.create("Double close test")
    c = service.add_comment(t.id, "Thread", author="alice")
    service.close_thread(c.id)

    r = client.post(f"/comments/{c.id}/close")
    assert r.status_code == 409


def test_reopen_thread_happy_path(client, service):
    """POST /comments/{id}/reopen on a closed thread returns 200
    with closed_at null."""
    t = service.create("Reopen test")
    c = service.add_comment(t.id, "Thread", author="alice")
    service.close_thread(c.id)

    r = client.post(f"/comments/{c.id}/reopen")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == c.id
    assert data["closed_at"] is None


def test_reopen_thread_open_returns_409(client, service):
    """POST /comments/{id}/reopen on an open thread returns 409."""
    t = service.create("Reopen open test")
    c = service.add_comment(t.id, "Open thread", author="alice")

    r = client.post(f"/comments/{c.id}/reopen")
    assert r.status_code == 409


def test_reopen_thread_nonexistent_returns_404(client):
    """POST /comments/{id}/reopen on a nonexistent comment returns 404."""
    r = client.post("/comments/99999/reopen")
    assert r.status_code == 404


def test_close_thread_nonexistent_returns_404(client):
    """POST /comments/{id}/close on a nonexistent comment returns 404."""
    r = client.post("/comments/99999/close")
    assert r.status_code == 404


def test_close_thread_ask_user_resumes_ticket(client, service):
    """AC1 via HTTP: Closing the last [ASK_USER] thread on a paused
    ticket auto-resumes it to paused_from."""
    t = service.create("HTTP resume test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)
    assert service.get(t.id).state is State.AWAITING_USER_REPLY

    ask = service.add_comment(t.id, "[ASK_USER]\n\nQ?", author="refine")
    r = client.post(f"/comments/{ask.id}/close")
    assert r.status_code == 200

    # Ticket resumed.
    assert service.get(t.id).state is State.READY


def test_close_thread_non_ask_user_on_paused_no_resume_via_http(client, service):
    """AC3 via HTTP: Closing a non-[ASK_USER] thread on a paused ticket
    does NOT resume."""
    t = service.create("HTTP non-ask")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    ask = service.add_comment(t.id, "[ASK_USER]\n\nQ?", author="refine")
    normal = service.add_comment(t.id, "Normal thread", author="alice")

    r = client.post(f"/comments/{normal.id}/close")
    assert r.status_code == 200
    assert service.get(t.id).state is State.AWAITING_USER_REPLY

    # Now close the ask thread → resumes.
    r = client.post(f"/comments/{ask.id}/close")
    assert r.status_code == 200
    assert service.get(t.id).state is State.READY


def test_close_thread_stays_paused_when_other_ask_user_open_via_http(client, service):
    """AC1 via HTTP: Two [ASK_USER] threads, close one — ticket stays
    paused."""
    t = service.create("HTTP multi-ask")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)

    c1 = service.add_comment(t.id, "[ASK_USER]\n\nQ1?", author="refine")
    c2 = service.add_comment(t.id, "[ASK_USER]\n\nQ2?", author="implement")

    r = client.post(f"/comments/{c1.id}/close")
    assert r.status_code == 200
    assert service.get(t.id).state is State.AWAITING_USER_REPLY

    r = client.post(f"/comments/{c2.id}/close")
    assert r.status_code == 200
    assert service.get(t.id).state is State.READY


def test_close_thread_on_non_paused_ticket_no_side_effect_via_http(client, service):
    """AC6 via HTTP: close_thread on a non-paused ticket returns 200
    without transitioning the ticket."""
    t = service.create("HTTP normal close")
    service.transition(t.id, State.READY)

    c = service.add_comment(t.id, "[ASK_USER]\n\nQ?", author="refine")
    r = client.post(f"/comments/{c.id}/close")
    assert r.status_code == 200
    assert service.get(t.id).state is State.READY


def test_add_comment_with_parent_id(client, service):
    """POST /tickets/{id}/comments with parent_id creates a reply."""
    t = service.create("Reply test")
    parent = service.add_comment(t.id, "Parent", author="alice")

    r = client.post(
        f"/tickets/{t.id}/comments",
        json={"body": "A reply", "parent_id": parent.id},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["body"] == "A reply"
    assert data["parent_id"] == parent.id


def test_add_comment_invalid_parent_returns_422(client, service):
    """POST /tickets/{id}/comments with invalid parent_id returns 422."""
    t = service.create("Bad parent test")

    r = client.post(
        f"/tickets/{t.id}/comments",
        json={"body": "Orphan reply", "parent_id": 99999},
    )
    # The service raises ValueError which the route maps to 400
    assert r.status_code in (400, 422)


# -- GET /active --------------------------------------------------------


def test_active_empty(client):
    """GET /active returns empty list when worker has no active items."""
    r = client.get("/active")
    assert r.status_code == 200
    assert r.json() == []


def test_active_with_items(client):
    """GET /active returns currently-processing tickets from the worker."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()

    class FakeWorker:
        _active = {
            "ticket-1": {"stage": "implement", "started_at": now},
            "ticket-2": {"stage": "refine", "started_at": now},
        }

    from robotsix_mill.runtime.deps import get_worker as _get_worker

    client.app.dependency_overrides[_get_worker] = lambda: FakeWorker()

    try:
        r = client.get("/active")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 2
        ids = {e["ticket_id"] for e in data}
        assert ids == {"ticket-1", "ticket-2"}
        for e in data:
            assert "stage" in e
            assert "started_at" in e
    finally:
        client.app.dependency_overrides.clear()


# -- GET /costs/by-agent -------------------------------------------------


def test_cost_by_agent_happy_path(client, monkeypatch):
    """GET /costs/by-agent returns aggregated cost data from Langfuse."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_by_name",
        lambda settings, lookback_hours, repo_config=None, max_tickets=None: [
            {"name": "refine", "count": 5, "totalCost": 0.12},
            {"name": "implement", "count": 3, "totalCost": 0.45},
        ],
    )

    r = client.get("/costs/by-agent")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    names = {e["name"] for e in data}
    assert names == {"refine", "implement"}


def test_cost_by_agent_clamp_low(client, monkeypatch):
    """GET /costs/by-agent?lookback_hours=0 clamps to 1.0."""
    captured: list[float] = []

    def fake_aggregate(settings, lookback_hours, repo_config=None, max_tickets=None):
        captured.append(lookback_hours)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_by_name",
        fake_aggregate,
    )

    r = client.get("/costs/by-agent?lookback_hours=0")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 1.0, f"expected clamped to 1.0, got {captured[0]}"


def test_cost_by_agent_clamp_high(client, monkeypatch):
    """GET /costs/by-agent?lookback_hours=200 clamps to 168.0."""
    captured: list[float] = []

    def fake_aggregate(settings, lookback_hours, repo_config=None, max_tickets=None):
        captured.append(lookback_hours)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_by_name",
        fake_aggregate,
    )

    r = client.get("/costs/by-agent?lookback_hours=200")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 168.0, f"expected clamped to 168.0, got {captured[0]}"


def test_cost_by_agent_max_tickets(client, monkeypatch):
    """GET /costs/by-agent?max_tickets=100 uses ticket-count mode."""
    captured: list[int | None] = []

    def fake_aggregate(settings, lookback_hours=24, max_tickets=None, repo_config=None):
        captured.append(max_tickets)
        return [{"name": "refine", "total_cost": 0.5, "trace_count": 2}]

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_by_name",
        fake_aggregate,
    )

    r = client.get("/costs/by-agent?max_tickets=100")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert len(captured) == 1
    assert captured[0] == 100


def test_cost_by_agent_max_tickets_clamp(client, monkeypatch):
    """GET /costs/by-agent?max_tickets=2000 clamps to 1000, ?max_tickets=0 clamps to 1."""
    captured: list[int | None] = []

    def fake_aggregate(settings, lookback_hours=24, max_tickets=None, repo_config=None):
        captured.append(max_tickets)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_by_name",
        fake_aggregate,
    )

    r = client.get("/costs/by-agent?max_tickets=2000")
    assert r.status_code == 200
    assert captured[-1] == 1000, f"expected clamped to 1000, got {captured[-1]}"

    r = client.get("/costs/by-agent?max_tickets=0")
    assert r.status_code == 200
    assert captured[-1] == 1, f"expected clamped to 1, got {captured[-1]}"


# -- GET /costs/most-expensive-ticket -----------------------------------


def test_most_expensive_ticket_happy_path(client, service, monkeypatch):
    """GET /costs/most-expensive-ticket returns ticket info from DB."""
    t = service.create("Most expensive ticket test")
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        lambda settings, lookback_hours, repo_config=None, max_tickets=None: {
            "session_id": t.id,
            "total_cost": 1.2345,
            "trace_count": 3,
        },
    )

    r = client.get("/costs/most-expensive-ticket")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert data["ticket_id"] == t.id
    assert data["cost_usd"] == 1.2345
    assert data["title"] == "Most expensive ticket test"


def test_most_expensive_ticket_null_when_disabled(client, monkeypatch):
    """GET /costs/most-expensive-ticket returns null when tracing disabled."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        lambda settings, lookback_hours, repo_config=None, max_tickets=None: None,
    )

    r = client.get("/costs/most-expensive-ticket")
    assert r.status_code == 200
    assert r.json() is None


def test_most_expensive_ticket_null_when_no_matching_ticket(client, monkeypatch):
    """GET /costs/most-expensive-ticket returns null when session has no DB ticket."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        lambda settings, lookback_hours, repo_config=None, max_tickets=None: {
            "session_id": "T-nonexistent",
            "total_cost": 0.5,
            "trace_count": 1,
        },
    )

    r = client.get("/costs/most-expensive-ticket")
    assert r.status_code == 200
    assert r.json() is None


def test_most_expensive_ticket_clamp_low(client, monkeypatch):
    """GET /costs/most-expensive-ticket?lookback_hours=0 clamps to 1.0."""
    captured: list[float] = []

    def fake(settings, lookback_hours, repo_config=None, max_tickets=None):
        captured.append(lookback_hours)
        return None

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        fake,
    )

    r = client.get("/costs/most-expensive-ticket?lookback_hours=0")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 1.0, f"expected clamped to 1.0, got {captured[0]}"


def test_most_expensive_ticket_clamp_high(client, monkeypatch):
    """GET /costs/most-expensive-ticket?lookback_hours=200 clamps to 168.0."""
    captured: list[float] = []

    def fake(settings, lookback_hours, repo_config=None, max_tickets=None):
        captured.append(lookback_hours)
        return None

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        fake,
    )

    r = client.get("/costs/most-expensive-ticket?lookback_hours=200")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 168.0, f"expected clamped to 168.0, got {captured[0]}"


def test_most_expensive_ticket_max_tickets(client, service, monkeypatch):
    """GET /costs/most-expensive-ticket?max_tickets=200 uses ticket-count mode."""
    t = service.create("Max tickets test")
    captured: list[int | None] = []

    def fake(settings, lookback_hours=24, max_tickets=None, repo_config=None):
        captured.append(max_tickets)
        return {"session_id": t.id, "total_cost": 0.999, "trace_count": 1}

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        fake,
    )

    r = client.get("/costs/most-expensive-ticket?max_tickets=200")
    assert r.status_code == 200
    data = r.json()
    assert data["ticket_id"] == t.id
    assert data["cost_usd"] == 0.999
    assert len(captured) == 1
    assert captured[0] == 200


def test_most_expensive_ticket_max_tickets_clamp(client, monkeypatch):
    """GET /costs/most-expensive-ticket?max_tickets=2000 clamps to 1000, ?max_tickets=0 clamps to 1."""
    captured: list[int | None] = []

    def fake(settings, lookback_hours=24, max_tickets=None, repo_config=None):
        captured.append(max_tickets)
        return None

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        fake,
    )

    r = client.get("/costs/most-expensive-ticket?max_tickets=2000")
    assert r.status_code == 200
    assert captured[-1] == 1000, f"expected clamped to 1000, got {captured[-1]}"

    r = client.get("/costs/most-expensive-ticket?max_tickets=0")
    assert r.status_code == 200
    assert captured[-1] == 1, f"expected clamped to 1, got {captured[-1]}"


# -- GET /costs/most-expensive-trace ------------------------------------


def test_most_expensive_trace_happy_path(client, monkeypatch):
    """GET /costs/most-expensive-trace returns the trace dict directly."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_trace",
        lambda settings, lookback_hours, repo_config=None, max_tickets=None: {
            "id": "trace-abc",
            "name": "implement",
            "total_cost": 0.9876,
            "timestamp": "2025-01-01T00:00:00Z",
            "session_id": "T-42",
        },
    )

    r = client.get("/costs/most-expensive-trace")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert data["id"] == "trace-abc"
    assert data["name"] == "implement"
    assert data["total_cost"] == 0.9876
    assert data["timestamp"] == "2025-01-01T00:00:00Z"
    assert data["session_id"] == "T-42"


def test_most_expensive_trace_null_when_disabled(client, monkeypatch):
    """GET /costs/most-expensive-trace returns null when tracing disabled."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_trace",
        lambda settings, lookback_hours, repo_config=None, max_tickets=None: None,
    )

    r = client.get("/costs/most-expensive-trace")
    assert r.status_code == 200
    assert r.json() is None


def test_most_expensive_trace_clamp_low(client, monkeypatch):
    """GET /costs/most-expensive-trace?lookback_hours=0 clamps to 1.0."""
    captured: list[float] = []

    def fake(settings, lookback_hours, repo_config=None, max_tickets=None):
        captured.append(lookback_hours)
        return None

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_trace",
        fake,
    )

    r = client.get("/costs/most-expensive-trace?lookback_hours=0")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 1.0, f"expected clamped to 1.0, got {captured[0]}"


def test_most_expensive_trace_clamp_high(client, monkeypatch):
    """GET /costs/most-expensive-trace?lookback_hours=200 clamps to 168.0."""
    captured: list[float] = []

    def fake(settings, lookback_hours, repo_config=None, max_tickets=None):
        captured.append(lookback_hours)
        return None

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_trace",
        fake,
    )

    r = client.get("/costs/most-expensive-trace?lookback_hours=200")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 168.0, f"expected clamped to 168.0, got {captured[0]}"


def test_most_expensive_trace_max_tickets(client, monkeypatch):
    """GET /costs/most-expensive-trace?max_tickets=50 uses ticket-count mode."""
    captured: list[int | None] = []

    def fake(settings, lookback_hours=24, max_tickets=None, repo_config=None):
        captured.append(max_tickets)
        return {
            "id": "abc",
            "name": "costly",
            "total_cost": 2.5,
            "timestamp": "x",
            "session_id": "s1",
        }

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_trace",
        fake,
    )

    r = client.get("/costs/most-expensive-trace?max_tickets=50")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "abc"
    assert data["total_cost"] == 2.5
    assert len(captured) == 1
    assert captured[0] == 50


def test_most_expensive_trace_max_tickets_clamp(client, monkeypatch):
    """GET /costs/most-expensive-trace?max_tickets=2000 clamps to 1000, ?max_tickets=0 clamps to 1."""
    captured: list[int | None] = []

    def fake(settings, lookback_hours=24, max_tickets=None, repo_config=None):
        captured.append(max_tickets)
        return None

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_trace",
        fake,
    )

    r = client.get("/costs/most-expensive-trace?max_tickets=2000")
    assert r.status_code == 200
    assert captured[-1] == 1000, f"expected clamped to 1000, got {captured[-1]}"

    r = client.get("/costs/most-expensive-trace?max_tickets=0")
    assert r.status_code == 200
    assert captured[-1] == 1, f"expected clamped to 1, got {captured[-1]}"


# -- GET /traces/recent --------------------------------------------------


def test_traces_recent_happy_path(client, monkeypatch):
    """GET /traces/recent returns serialised trace dicts from Langfuse."""
    fake_traces = [
        {
            "id": f"trace-{i}",
            "name": f"trace-name-{i}",
            "timestamp": "2025-01-01T00:00:00Z",
            "sessionId": f"session-{i}",
            "totalCost": 0.01 * i,
            "userId": "user-1",
            "extraField": "should-be-stripped",
        }
        for i in range(5)
    ]

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.list_recent_traces",
        lambda settings, limit, min_cost=None, max_cost=None: fake_traces,
    )

    r = client.get("/traces/recent")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 5
    for t in data:
        # Only the serialised subset of keys is returned.
        for key in ("id", "name", "timestamp", "sessionId", "totalCost"):
            assert key in t, f"trace missing key '{key}': {t}"
        assert "extraField" not in t, "unexpected key leaked through serialization"


def test_traces_recent_clamp_low(client, monkeypatch):
    """GET /traces/recent?limit=0 clamps to 1."""
    captured: list[int] = []

    def fake_list(settings, limit, min_cost=None, max_cost=None):
        captured.append(limit)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.list_recent_traces",
        fake_list,
    )

    r = client.get("/traces/recent?limit=0")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 1, f"expected clamped to 1, got {captured[0]}"


def test_traces_recent_clamp_high(client, monkeypatch):
    """GET /traces/recent?limit=200 clamps to 50."""
    captured: list[int] = []

    def fake_list(settings, limit, min_cost=None, max_cost=None):
        captured.append(limit)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.list_recent_traces",
        fake_list,
    )

    r = client.get("/traces/recent?limit=200")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 50, f"expected clamped to 50, got {captured[0]}"


# -- POST /survey -------------------------------------------------------


def test_survey_fire_and_forget(client, monkeypatch):
    """POST /survey returns 202 immediately and runs the survey in a
    background thread — must not block on the LLM call."""
    from robotsix_mill import survey_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_survey(session_id: str = "", repo_config=None):
        ran.set()
        release.wait(5)
        return _R()

    monkeypatch.setattr(survey_runner, "run_survey_pass", slow_survey)

    r = client.post("/survey")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5), "survey did not start in background"
    release.set()  # let the daemon thread finish


# -- POST /module-curator -----------------------------------------------


def test_module_curator_fire_and_forget(client, monkeypatch):
    """POST /module-curator returns 202 immediately and runs the pass in
    a background thread (run-now surface for the daily module-curator)."""
    from robotsix_mill import module_curator_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_curator(session_id: str = "", repo_config=None):
        ran.set()
        release.wait(5)
        return _R()

    monkeypatch.setattr(module_curator_runner, "run_module_curator_pass", slow_curator)

    r = client.post("/module-curator")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5), "module-curator did not start in background"
    release.set()  # let the daemon thread finish


# -- POST /tickets/{id}/transition (happy path) -------------------------


def test_transition_happy_path_draft_to_ready(client, service):
    """POST /tickets/{id}/transition with {'state': 'ready'} transitions
    a draft ticket to ready and returns the updated ticket."""
    t = service.create("Transition me")

    r = client.post(
        f"/tickets/{t.id}/transition",
        json={"state": "ready", "note": "looks good"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id
    assert data["title"] == "Transition me"
    assert data["state"] == "ready"
    assert "cost_usd" in data


# -- GET /costs/trend ----------------------------------------------------


def test_cost_trend_happy_path(client, monkeypatch):
    """GET /costs/trend returns bucketed trend data."""
    fake_buckets = [
        {"ts": "2025-06-24T00:00:00Z", "total_cost": 0.1234, "trace_count": 5},
        {"ts": "2025-06-24T01:00:00Z", "total_cost": 0.0567, "trace_count": 3},
    ]
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_trend",
        lambda settings, lookback_hours, repo_config=None, max_tickets=None: (
            fake_buckets
        ),
    )

    r = client.get("/costs/trend")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict)
    assert "buckets" in data
    assert len(data["buckets"]) == 2
    assert data["buckets"][0]["ts"] == "2025-06-24T00:00:00Z"
    assert data["buckets"][0]["total_cost"] == 0.1234
    assert data["buckets"][0]["trace_count"] == 5


def test_cost_trend_clamp_low(client, monkeypatch):
    """GET /costs/trend?lookback_hours=0 clamps to 1.0."""
    captured: list[float] = []

    def fake_trend(settings, lookback_hours, repo_config=None, max_tickets=None):
        captured.append(lookback_hours)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_trend",
        fake_trend,
    )

    r = client.get("/costs/trend?lookback_hours=0")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 1.0, f"expected clamped to 1.0, got {captured[0]}"


def test_cost_trend_clamp_high(client, monkeypatch):
    """GET /costs/trend?lookback_hours=200 clamps to 168.0."""
    captured: list[float] = []

    def fake_trend(settings, lookback_hours, repo_config=None, max_tickets=None):
        captured.append(lookback_hours)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_trend",
        fake_trend,
    )

    r = client.get("/costs/trend?lookback_hours=200")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 168.0, f"expected clamped to 168.0, got {captured[0]}"


def test_cost_trend_max_tickets(client, monkeypatch):
    """GET /costs/trend?max_tickets=20 uses ticket-count mode."""
    captured: list[int | None] = []

    def fake_trend(settings, lookback_hours=24, max_tickets=None, repo_config=None):
        captured.append(max_tickets)
        return [{"ts": "2025-01-01T00:00:00Z", "total_cost": 1.0, "trace_count": 1}]

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_trend",
        fake_trend,
    )

    r = client.get("/costs/trend?max_tickets=20")
    assert r.status_code == 200
    data = r.json()
    assert "buckets" in data
    assert len(captured) == 1
    assert captured[0] == 20


def test_cost_trend_max_tickets_clamp(client, monkeypatch):
    """GET /costs/trend?max_tickets=2000 clamps to 1000, ?max_tickets=0 clamps to 1."""
    captured: list[int | None] = []

    def fake_trend(settings, lookback_hours=24, max_tickets=None, repo_config=None):
        captured.append(max_tickets)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_trend",
        fake_trend,
    )

    r = client.get("/costs/trend?max_tickets=2000")
    assert r.status_code == 200
    assert captured[-1] == 1000, f"expected clamped to 1000, got {captured[-1]}"

    r = client.get("/costs/trend?max_tickets=0")
    assert r.status_code == 200
    assert captured[-1] == 1, f"expected clamped to 1, got {captured[-1]}"


def test_cost_trend_empty_when_disabled(client, monkeypatch):
    """GET /costs/trend returns empty buckets when data is empty."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_trend",
        lambda settings, lookback_hours, repo_config=None, max_tickets=None: [],
    )

    r = client.get("/costs/trend")
    assert r.status_code == 200
    data = r.json()
    assert data == {"buckets": []}


# -- POST /tickets/{id}/mark-done ---------------------------------------


def test_mark_done_happy_path(client, service):
    """POST /tickets/{id}/mark-done transitions a draft ticket to done
    and returns the updated ticket."""
    t = service.create("Mark done test")

    r = client.post(
        f"/tickets/{t.id}/mark-done",
        json={"note": "done manually"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id
    assert data["title"] == "Mark done test"
    assert data["state"] == "done"


# -- WebSocket /ws/board (live board auto-refresh) ----------------------


def test_ws_board_connects_and_sends_initial_list(client):
    """The board's live-refresh WebSocket must accept the handshake and
    push an initial ``ticket_list``. Regression: /ws/board was orphaned in
    an unincluded module (never registered → 403) and used Request-based
    deps that can't resolve for a WebSocket scope, silently breaking the
    board's auto-refresh."""
    with client.websocket_connect("/ws/board?show_closed=false") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ticket_list"
        assert isinstance(msg["tickets"], list)
