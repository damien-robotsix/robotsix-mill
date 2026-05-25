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
def client(settings):
    """Reusable TestClient wired to the same lifespan as test_api.py."""
    with TestClient(create_app(settings)) as c:
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
    assert len(data) >= 2, f"expected >=2 events (created + transition), got {len(data)}"
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
        lambda settings, lookback_hours: [
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

    def fake_aggregate(settings, lookback_hours):
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

    def fake_aggregate(settings, lookback_hours):
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


# -- GET /costs/most-expensive-ticket -----------------------------------

def test_most_expensive_ticket_happy_path(client, service, monkeypatch):
    """GET /costs/most-expensive-ticket returns ticket info from DB."""
    t = service.create("Most expensive ticket test")
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        lambda settings, lookback_hours: {
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
        lambda settings, lookback_hours: None,
    )

    r = client.get("/costs/most-expensive-ticket")
    assert r.status_code == 200
    assert r.json() is None


def test_most_expensive_ticket_null_when_no_matching_ticket(client, monkeypatch):
    """GET /costs/most-expensive-ticket returns null when session has no DB ticket."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_ticket",
        lambda settings, lookback_hours: {
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

    def fake(settings, lookback_hours):
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

    def fake(settings, lookback_hours):
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


# -- GET /costs/most-expensive-trace ------------------------------------

def test_most_expensive_trace_happy_path(client, monkeypatch):
    """GET /costs/most-expensive-trace returns the trace dict directly."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.most_expensive_trace",
        lambda settings, lookback_hours: {
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
        lambda settings, lookback_hours: None,
    )

    r = client.get("/costs/most-expensive-trace")
    assert r.status_code == 200
    assert r.json() is None


def test_most_expensive_trace_clamp_low(client, monkeypatch):
    """GET /costs/most-expensive-trace?lookback_hours=0 clamps to 1.0."""
    captured: list[float] = []

    def fake(settings, lookback_hours):
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

    def fake(settings, lookback_hours):
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

    def slow_survey():
        ran.set()
        release.wait(5)
        return _R()

    monkeypatch.setattr(survey_runner, "run_survey_pass", slow_survey)

    r = client.post("/survey")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5), "survey did not start in background"
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
        lambda settings, lookback_hours: fake_buckets,
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

    def fake_trend(settings, lookback_hours):
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

    def fake_trend(settings, lookback_hours):
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


def test_cost_trend_empty_when_disabled(client, monkeypatch):
    """GET /costs/trend returns empty buckets when data is empty."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.aggregate_cost_trend",
        lambda settings, lookback_hours: [],
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
