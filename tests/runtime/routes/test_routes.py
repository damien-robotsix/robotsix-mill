"""Dedicated tests for previously-untested route handlers.

Covers the 7 endpoint groups that had zero HTTP-level coverage in the
existing 1479-line ``tests/runtime/test_api.py``:

* ``GET /tickets/{id}/history``
* ``GET /tickets/{id}/comments``
* ``GET /active``
* ``GET /traces/recent``
* ``POST /survey``
* ``POST /tickets/{id}/transition`` (happy-path only; error cases are
  already tested elsewhere)
"""

from __future__ import annotations

import contextlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.models import TicketKind
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


def test_active_repo_id_meta_filters_by_board(client):
    """GET /active?repo_id=meta returns 200 and filters on board_id == 'meta'.

    ``meta`` is a synthetic board id, not a registered repo, so it must
    skip the repo-registry validation (mirroring the /traces handler).
    """
    from types import SimpleNamespace

    tickets = {
        "ticket-meta": SimpleNamespace(board_id="meta"),
        "ticket-other": SimpleNamespace(board_id="test-board"),
    }

    class FakeWorker:
        _active = {
            "ticket-meta": {"stage": "implement", "started_at": "t0"},
            "ticket-other": {"stage": "refine", "started_at": "t0"},
        }
        ctx = SimpleNamespace(service=SimpleNamespace(get=tickets.get))

    from robotsix_mill.runtime.deps import get_worker as _get_worker

    client.app.dependency_overrides[_get_worker] = lambda: FakeWorker()

    try:
        r = client.get("/active?repo_id=meta")
        assert r.status_code == 200
        data = r.json()
        ids = {e["ticket_id"] for e in data}
        assert ids == {"ticket-meta"}
    finally:
        client.app.dependency_overrides.clear()


def test_active_unknown_repo_returns_400(client):
    """GET /active?repo_id=<unknown> still returns 400."""
    r = client.get("/active?repo_id=definitely-not-a-repo")
    assert r.status_code == 400
    assert "Unknown repo" in r.json()["detail"]


# -- GET /tickets/{id}/cost-breakdown -----------------------------------


def test_cost_breakdown_happy_path(client, service, monkeypatch):
    """GET /tickets/{id}/cost-breakdown returns available=True with the
    per-trace rows from Langfuse."""
    t = service.create("Cost breakdown test")
    rows = [
        {
            "name": "refine",
            "cost": 0.12,
            "at": "2025-01-01T00:00:00Z",
            "trace_id": "tr-1",
            "latency": 1.0,
            "model": "x",
        }
    ]
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, ticket_id, repo_config=None: rows,
    )

    r = client.get(f"/tickets/{t.id}/cost-breakdown")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data["traces"] == rows


def test_cost_breakdown_unavailable_when_langfuse_none(client, service, monkeypatch):
    """GET /tickets/{id}/cost-breakdown returns available=False when
    session_traces yields None (tracing disabled)."""
    t = service.create("Cost breakdown unavailable")
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_traces",
        lambda settings, ticket_id, repo_config=None: None,
    )

    r = client.get(f"/tickets/{t.id}/cost-breakdown")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is False
    assert data["traces"] == []


def test_cost_breakdown_404_missing_ticket(client):
    """GET /tickets/{nonexistent}/cost-breakdown returns 404 before any
    Langfuse call."""
    r = client.get("/tickets/does-not-exist/cost-breakdown")
    assert r.status_code == 404


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
            "observations": [],
        }
        for i in range(5)
    ]

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.list_recent_traces",
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
        assert "observationSummary" in t, "observation summary missing"
        obs_sum = t["observationSummary"]
        assert "model" in obs_sum
        assert "input_tokens" in obs_sum
        assert "output_tokens" in obs_sum
        assert "tool_calls" in obs_sum


def test_traces_recent_clamp_low(client, monkeypatch):
    """GET /traces/recent?limit=0 clamps to 1."""
    captured: list[int] = []

    def fake_list(settings, limit, min_cost=None, max_cost=None):
        captured.append(limit)
        return []

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.list_recent_traces",
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
        "robotsix_mill.langfuse.client.list_recent_traces",
        fake_list,
    )

    r = client.get("/traces/recent?limit=200")
    assert r.status_code == 200
    assert len(captured) == 1
    assert captured[0] == 50, f"expected clamped to 50, got {captured[0]}"


def test_traces_detail_happy_path(client, monkeypatch):
    """GET /traces/{trace_id} returns full trace detail with observations."""
    fake_detail = {
        "id": "trace-42",
        "name": "refine",
        "sessionId": "session-1",
        "totalCost": 1.63,
        "observations": [
            {
                "name": "chat completion",
                "type": "GENERATION",
                "model": "openai/gpt-4o",
                "usage": {"input": 5000, "output": 1200},
            },
            {
                "name": "read_file",
                "type": "SPAN",
                "level": "DEFAULT",
            },
        ],
    }

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, trace_id, repo_config=None: fake_detail,
    )

    r = client.get("/traces/trace-42")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "trace-42"
    assert data["name"] == "refine"
    assert len(data["observations"]) == 2
    assert data["observations"][0]["model"] == "openai/gpt-4o"


def test_traces_detail_not_found(client, monkeypatch):
    """GET /traces/{trace_id} returns 404 when Langfuse returns None."""
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.fetch_trace_detail",
        lambda settings, trace_id, repo_config=None: None,
    )

    r = client.get("/traces/nonexistent")
    assert r.status_code == 404


# -- POST /survey -------------------------------------------------------


def test_survey_fire_and_forget(client, monkeypatch):
    """POST /survey returns 202 immediately and runs the survey in a
    background thread — must not block on the LLM call."""
    from robotsix_mill.runners import periodic_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_survey(session_id: str = "", repo_config=None):
        ran.set()
        release.wait(5)
        return _R()

    monkeypatch.setattr(periodic_runner, "run_survey_pass", slow_survey)

    r = client.post("/survey")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5), "survey did not start in background"
    release.set()  # let the daemon thread finish


# -- POST /module-curator -----------------------------------------------


def test_module_curator_fire_and_forget(client, monkeypatch):
    """POST /module-curator returns 202 immediately and runs the pass in
    a background thread (run-now surface for the daily module-curator)."""
    from robotsix_mill.runners import periodic_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_curator(session_id: str = "", repo_config=None):
        ran.set()
        release.wait(5)
        return _R()

    monkeypatch.setattr(periodic_runner, "run_module_curator_pass", slow_curator)

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


# -- POST /tickets/{id}/abandon-epic ------------------------------------


def test_abandon_epic_happy_path(client, service):
    """POST /tickets/{id}/abandon-epic transitions an EPIC_OPEN epic
    to EPIC_CLOSED and returns 200."""
    epic = service.create("Abandon me", kind=TicketKind.EPIC)
    assert epic.state is State.EPIC_OPEN

    r = client.post(
        f"/tickets/{epic.id}/abandon-epic",
        json={"actor": "tester"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] == epic.id
    assert data["state"] == "epic_closed"


def test_abandon_epic_rejects_non_epic_open(client, service):
    """POST /tickets/{id}/abandon-epic returns 422 when the ticket is
    not in EPIC_OPEN state."""
    t = service.create("Regular task")

    r = client.post(
        f"/tickets/{t.id}/abandon-epic",
        json={"actor": "tester"},
    )
    assert r.status_code == 422
    assert "not an open epic" in r.json()["detail"]


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


# -- _make_background_pass unit tests -----------------------------------


class _FakeRegistry:
    """Minimal fake RunRegistry for testing _make_background_pass."""

    def __init__(self, entries: list[dict] | None = None):
        self.starts: list[tuple] = []
        self.oks: list[tuple] = []
        self.errors: list[tuple] = []
        self._next_id = 1
        self._entries = entries or []

    def start(self, kind: str, repo_id: str = "") -> int:
        rid = self._next_id
        self._next_id += 1
        self.starts.append((kind, repo_id, rid))
        return rid

    def finish_ok(self, run_id: int, summary: str) -> None:
        self.oks.append((run_id, summary))

    def finish_error(self, run_id: int, error: str) -> None:
        self.errors.append((run_id, error))

    def list_all(self) -> list[dict]:
        return list(self._entries)


class _FakeRepos:
    """Single-repo fake so _resolve_agent_run_repos returns [None]."""

    def __init__(self):
        self.repos = {}


class _FakeAppState:
    def __init__(self, repos: _FakeRepos):
        self.repos = repos


class _FakeRequest:
    def __init__(self, repos: _FakeRepos):
        self.app = type("_App", (), {"state": _FakeAppState(repos)})()


def _wait_for_thread(thread: threading.Thread, timeout: float = 5.0) -> None:
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "background thread did not finish"


def _wait_for_pass(registry: "_FakeRegistry", timeout: float = 5.0) -> None:
    """Block until the background pass records a terminal result (ok or
    error) in *registry*.

    Deterministic replacement for "find the daemon thread by name and
    join it": the runner here is a fast fake, so the thread often
    finishes — and leaves ``threading.enumerate()`` — before the test
    inspects it, which made the name-lookup flaky (it raised
    "daemon thread X not found" or read the registry before the thread
    had recorded). Waiting on the observable side-effect has no race.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if registry.oks or registry.errors:
            return
        time.sleep(0.005)
    raise AssertionError("background pass did not record a result in time")


def test_factory_default_tracing_handler(monkeypatch):
    """A factory handler with default settings launches a daemon thread
    that calls the runner with session_id and records ok/error."""
    import sys
    from robotsix_mill.runtime.routes._passes import _make_background_pass

    # -- inject a fake runner module ----------------------------------------
    class _FakeResult:
        drafts_created: list = [{"id": "D-1"}, {"id": "D-2"}]

    def _fake_runner(session_id: str = "", repo_config=None):
        return _FakeResult()

    fake_mod = type("_FakeMod", (), {"run_test_pass": staticmethod(_fake_runner)})()
    sys.modules["robotsix_mill.runners.test_factory_runner"] = fake_mod

    # -- stub the tracing helpers so we don't need Langfuse -----------------
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.make_session_id",
        lambda kind: f"session-{kind}",
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.tracing.start_ticket_root_span",
        lambda session_id, stage, repo_config=None: contextlib.nullcontext(),
    )

    handler = _make_background_pass(
        kind="test-factory",
        runner_module="robotsix_mill.runners.test_factory_runner",
        runner_func="run_test_pass",
        docstring="Test handler.",
    )

    registry = _FakeRegistry()
    repos = _FakeRepos()
    request = _FakeRequest(repos)

    # Call the handler — it spawns the thread and returns immediately.
    resp = handler(repo_id=None, request=request, registry=registry)
    assert resp == {"status": "started"}

    # Wait for the background pass to record its result (race-free).
    _wait_for_pass(registry)

    # Registry assertions.
    assert len(registry.starts) == 1
    kind, repo_id, run_id = registry.starts[0]
    assert kind == "test-factory"
    assert repo_id == ""  # rc.repo_id is "" when rc is None

    assert len(registry.oks) == 1
    assert registry.oks[0][0] == run_id
    assert registry.oks[0][1] == "Created 2 drafts: D-1, D-2"
    assert len(registry.errors) == 0


def test_factory_no_tracing_handler():
    """uses_tracing=False skips session_id and tracing helpers."""
    import sys
    from robotsix_mill.runtime.routes._passes import _make_background_pass

    class _FakeResult:
        drafts_created: list = [{"id": "D-99"}]

    def _fake_runner(repo_config=None):
        return _FakeResult()

    fake_mod = type("_FakeMod", (), {"run_notrace_pass": staticmethod(_fake_runner)})()
    sys.modules["robotsix_mill.runners.test_notrace_runner"] = fake_mod

    handler = _make_background_pass(
        kind="notrace",
        runner_module="robotsix_mill.runners.test_notrace_runner",
        runner_func="run_notrace_pass",
        docstring="No tracing.",
        uses_tracing=False,
    )

    registry = _FakeRegistry()
    repos = _FakeRepos()
    resp = handler(repo_id=None, request=_FakeRequest(repos), registry=registry)
    assert resp == {"status": "started"}

    _wait_for_pass(registry)

    assert len(registry.starts) == 1
    assert registry.starts[0][0] == "notrace"
    assert len(registry.oks) == 1
    assert registry.oks[0][1] == "Created 1 drafts: D-99"


def test_factory_custom_summary_builder():
    """Custom summary_builder replaces _default_summary."""
    import sys
    from robotsix_mill.runtime.routes._passes import _make_background_pass

    class _FakeResult:
        summary = "all clear"
        drafts_created: list = []

    def _fake_runner(repo_config=None):
        return _FakeResult()

    fake_mod = type("_FakeMod", (), {"run_custom_pass": staticmethod(_fake_runner)})()
    sys.modules["robotsix_mill.runners.test_custom_runner"] = fake_mod

    handler = _make_background_pass(
        kind="custom",
        runner_module="robotsix_mill.runners.test_custom_runner",
        runner_func="run_custom_pass",
        docstring="Custom summary.",
        uses_tracing=False,
        summary_builder=lambda r: r.summary or "fallback",
    )

    registry = _FakeRegistry()
    repos = _FakeRepos()
    resp = handler(repo_id=None, request=_FakeRequest(repos), registry=registry)
    assert resp == {"status": "started"}

    _wait_for_pass(registry)

    assert len(registry.oks) == 1
    assert registry.oks[0][1] == "all clear"


def test_factory_extra_runner_kwargs():
    """extra_runner_kwargs forwards extra kwargs to the runner."""
    import sys
    from robotsix_mill.runtime.routes._passes import _make_background_pass

    received_kwargs: dict = {}

    class _FakeResult:
        drafts_created: list = []

    def _fake_runner(repo_config=None, **kwargs):
        received_kwargs.update(kwargs)
        return _FakeResult()

    fake_mod = type("_FakeMod", (), {"run_extra_pass": staticmethod(_fake_runner)})()
    sys.modules["robotsix_mill.runners.test_extra_runner"] = fake_mod

    handler = _make_background_pass(
        kind="extra",
        runner_module="robotsix_mill.runners.test_extra_runner",
        runner_func="run_extra_pass",
        docstring="Extra kwargs.",
        uses_tracing=False,
        extra_runner_kwargs=lambda req: {"alpha": 1, "beta": "two"},
    )

    registry = _FakeRegistry()
    repos = _FakeRepos()
    resp = handler(repo_id=None, request=_FakeRequest(repos), registry=registry)
    assert resp == {"status": "started"}

    _wait_for_pass(registry)

    assert received_kwargs == {"alpha": 1, "beta": "two"}


def test_factory_error_path():
    """When the runner raises, registry.finish_error is called."""
    import sys
    from robotsix_mill.runtime.routes._passes import _make_background_pass

    def _failing_runner(repo_config=None):
        raise RuntimeError("simulated crash")

    fake_mod = type("_FakeMod", (), {"run_fail_pass": staticmethod(_failing_runner)})()
    sys.modules["robotsix_mill.runners.test_fail_runner"] = fake_mod

    handler = _make_background_pass(
        kind="fail",
        runner_module="robotsix_mill.runners.test_fail_runner",
        runner_func="run_fail_pass",
        docstring="Always fails.",
        uses_tracing=False,
    )

    registry = _FakeRegistry()
    repos = _FakeRepos()
    resp = handler(repo_id=None, request=_FakeRequest(repos), registry=registry)
    assert resp == {"status": "started"}

    _wait_for_pass(registry)

    assert len(registry.starts) >= 1
    assert len(registry.errors) >= 1
    assert "simulated crash" in registry.errors[0][1]


def test_factory_thread_is_daemon():
    """Generated handler MUST spawn a daemon thread so the process can
    exit without waiting for it."""
    import sys
    from robotsix_mill.runtime.routes._passes import _make_background_pass

    hold = threading.Event()

    class _FakeResult:
        drafts_created: list = []

    def _blocking_runner(repo_config=None):
        hold.wait(5)  # wait until we release
        return _FakeResult()

    fake_mod = type(
        "_FakeMod", (), {"run_block_pass": staticmethod(_blocking_runner)}
    )()
    sys.modules["robotsix_mill.runners.test_block_runner"] = fake_mod

    handler = _make_background_pass(
        kind="block",
        runner_module="robotsix_mill.runners.test_block_runner",
        runner_func="run_block_pass",
        docstring="Blocks.",
        uses_tracing=False,
    )

    registry = _FakeRegistry()
    repos = _FakeRepos()
    handler(repo_id=None, request=_FakeRequest(repos), registry=registry)

    found = None
    for t in threading.enumerate():
        if t.name == "block-pass":
            found = t
            break

    assert found is not None, "background thread was not spawned"
    assert found.daemon is True, "background thread must be daemon=True"

    hold.set()  # release the thread
    _wait_for_thread(found)


# -- list_runs: synthetic meta board ------------------------------------


def test_list_runs_meta_board_no_400_and_filters_meta_entries():
    """``GET /runs?repo_id=meta`` must not raise 400 (the meta board is a
    valid synthetic board) and returns only meta-tagged entries (plus any
    legacy entries with an empty repo_id)."""
    from robotsix_mill.runtime.routes._traces import list_runs

    entries = [
        {"id": "m1", "kind": "meta", "repo_id": "meta", "started_at": "2026-01-02"},
        {"id": "r1", "kind": "audit", "repo_id": "repo-a", "started_at": "2026-01-01"},
        {"id": "g1", "kind": "health", "repo_id": "", "started_at": "2026-01-03"},
    ]
    registry = _FakeRegistry(entries=entries)
    repos = _FakeRepos()  # "meta" deliberately absent from repos.repos
    result = list_runs(repo_id="meta", request=_FakeRequest(repos), registry=registry)
    ids = {e["id"] for e in result}
    assert ids == {"m1", "g1"}  # meta-tagged + empty-repo_id, never repo-a


# -- POST /tickets/{id}/transition -------------------------------------


# -- POST /tickets/{id}/migrate ------------------------------------------


@pytest.fixture
def migrate_client(settings):
    """TestClient over TWO boards, with the repos-config singleton
    aligned so ``TicketService.migrate`` validates the same registry
    the routes resolve against."""
    import robotsix_mill.config as _cfg
    from robotsix_mill.config import RepoConfig, ReposRegistry
    from robotsix_mill.core import db as _db

    registry = ReposRegistry(
        repos={
            "test-repo": RepoConfig(
                repo_id="test-repo",
                board_id="test-board",
                langfuse_project_name="proj-a",
                langfuse_public_key="pk-a",
                langfuse_secret_key="sk-a",
            ),
            "other-repo": RepoConfig(
                repo_id="other-repo",
                board_id="other-board",
                langfuse_project_name="proj-b",
                langfuse_public_key="pk-b",
                langfuse_secret_key="sk-b",
            ),
        }
    )
    _cfg._repos_config = registry
    _db.init_db(settings, board_id="other-board")
    with TestClient(create_app(registry, settings)) as c:
        yield c
    _cfg._repos_config = None


def test_migrate_ticket_happy_path(migrate_client, service):
    """POST /tickets/{id}/migrate moves the ticket to the target board
    and returns it as a DRAFT there."""
    t = service.create("Misrouted", "fix belongs to other-repo")

    r = migrate_client.post(
        f"/tickets/{t.id}/migrate",
        json={"repo_id": "other-repo", "note": "belongs there"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["board_id"] == "other-board"
    assert data["state"] == "draft"

    r2 = migrate_client.get(f"/tickets/{t.id}/history")
    assert any("migrated from board" in (e["note"] or "") for e in r2.json())


def test_migrate_ticket_unknown_repo_400(migrate_client, service):
    t = service.create("Misrouted", "body")
    r = migrate_client.post(
        f"/tickets/{t.id}/migrate", json={"repo_id": "no-such-repo"}
    )
    assert r.status_code == 400


def test_migrate_ticket_404(migrate_client):
    r = migrate_client.post(
        "/tickets/nonexistent/migrate", json={"repo_id": "other-repo"}
    )
    assert r.status_code == 404


def test_migrate_epic_via_route(migrate_client, service):
    """POST /tickets/{epic_id}/migrate moves an epic (and its subtree)
    to the target board and returns the root as a DRAFT there."""
    epic = service.create("Epic on wrong board", kind=TicketKind.EPIC)
    child = service.create("Epic child", parent_id=epic.id)

    r = migrate_client.post(
        f"/tickets/{epic.id}/migrate",
        json={"repo_id": "other-repo", "note": "mis-filed"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["board_id"] == "other-board"
    assert data["state"] == "draft"

    # Both tickets landed on the target board.
    for tid in [epic.id, child.id]:
        r2 = migrate_client.get(f"/tickets/{tid}")
        assert r2.status_code == 200
        assert r2.json()["board_id"] == "other-board"

    # History includes migration note.
    r3 = migrate_client.get(f"/tickets/{epic.id}/history")
    assert any("migrated from board" in (e["note"] or "") for e in r3.json())


# -- GET /worker-status -------------------------------------------------


def test_worker_status_shape(client):
    """GET /worker-status returns live queue/_pending/task-health introspection."""
    r = client.get("/worker-status")
    assert r.status_code == 200
    d = r.json()
    for key in ("queues", "pending", "tasks_total", "tasks_alive", "dead_tasks"):
        assert key in d, f"missing {key}: {d}"
    assert isinstance(d["queues"], dict)
    assert isinstance(d["pending"], list)
    assert isinstance(d["dead_tasks"], list)


# -- Background-pass route coverage (19 previously-untested handlers) ---


class _FakePassResult:
    """Fake result with every attribute any pass summary builder may read."""

    drafts_created: list = [{"id": "D-1"}]
    # trace-health
    unsessioned_count: int = 0
    name_missing_count: int = 0
    total_traces: int = 10
    window_start: str = "2025-01-01"
    window_end: str = "2025-01-02"
    draft_created: bool = False
    # langfuse-cleanup
    project: str = "test-project"
    traces_before: int = 100
    traces_deleted: int = 10
    # meta
    extraction_drafts_created: list = []
    alignment_drafts_created: list = []


# (route_path, dotted_module_to_patch, attr_name_to_patch)
BG_PASS_ROUTES = [
    # -- 13 factory-based routes (all use _make_background_pass) ----------
    ("/audit", "robotsix_mill.runners.periodic_runner", "run_audit_pass"),
    ("/bc-check", "robotsix_mill.runners.periodic_runner", "run_bc_check_pass"),
    (
        "/completeness-check",
        "robotsix_mill.runners.periodic_runner",
        "run_completeness_check_pass",
    ),
    (
        "/agent-check",
        "robotsix_mill.runners.periodic_runner",
        "run_agent_check_pass",
    ),
    ("/health-check", "robotsix_mill.runners.periodic_runner", "run_health_pass"),
    ("/test-gap", "robotsix_mill.runners.periodic_runner", "run_test_gap_pass"),
    (
        "/copy-paste",
        "robotsix_mill.runners.periodic_runner",
        "run_copy_paste_pass",
    ),
    (
        "/forge-parity",
        "robotsix_mill.runners.periodic_runner",
        "run_forge_parity_pass",
    ),
    (
        "/config-sync",
        "robotsix_mill.runners.periodic_runner",
        "run_config_sync_pass",
    ),
    (
        "/member-sync",
        "robotsix_mill.runners.member_sync_runner",
        "run_member_sync_pass",
    ),
    (
        "/trace-review",
        "robotsix_mill.runners.trace_review_runner",
        "run_trace_review_pass",
    ),
    (
        "/roadmap-sync",
        "robotsix_mill.runners.roadmap_sync_runner",
        "run_roadmap_sync_pass",
    ),
    # -- 6 custom handlers -----------------------------------------------
    (
        "/trace-health",
        "robotsix_mill.runners.trace_health_runner",
        "run_trace_health_check",
    ),
    (
        "/langfuse-cleanup",
        "robotsix_mill.runners.langfuse_cleanup_runner",
        "run_langfuse_cleanup_pass",
    ),
    ("/meta", "robotsix_mill.meta.runner", "run_meta_pass"),
    (
        "/run-health",
        "robotsix_mill.runners.run_health_runner",
        "run_run_health_pass",
    ),
    (
        "/state-sync",
        "robotsix_mill.runners.periodic_runner",
        "run_state_sync_pass",
    ),
    (
        "/env-doc-sync",
        "robotsix_mill.runners.periodic_runner",
        "run_env_doc_sync_pass",
    ),
]


@pytest.mark.parametrize("route, target_module, target_attr", BG_PASS_ROUTES)
def test_bg_pass_route_success(client, monkeypatch, route, target_module, target_attr):
    """Every background-pass route returns 202 {"status": "started"} and
    invokes its runner in a background thread."""
    import importlib

    ran = threading.Event()
    release = threading.Event()

    def fake_runner(**kwargs):
        ran.set()
        release.wait(5)
        return _FakePassResult()

    mod = importlib.import_module(target_module)
    monkeypatch.setattr(mod, target_attr, fake_runner)

    r = client.post(route)
    assert r.status_code == 202, f"{route}: expected 202, got {r.status_code}"
    assert r.json() == {"status": "started"}
    assert ran.wait(5), f"{route}: runner was not invoked in background"
    release.set()


# Routes whose handler calls _resolve_agent_run_repos *synchronously*
# (outside the daemon thread), so an unknown repo_id → 400 to the client.
_REPO_ID_ERROR_ROUTES = [
    "/trace-health",
    "/langfuse-cleanup",
]


@pytest.mark.parametrize("route", _REPO_ID_ERROR_ROUTES)
def test_bg_pass_route_unknown_repo_400(client, route):
    """Routes that resolve repo_id synchronously return 400 for unknown repos."""
    r = client.post(f"{route}?repo_id=unknown")
    assert r.status_code == 400, f"{route}: expected 400, got {r.status_code}"
    assert "Unknown repo" in r.json()["detail"]


# -- Health endpoint coverage -------------------------------------------


class _FakeLangfuseClient:
    """A non-None sentinel so _build_read_client → "configured"."""


def test_health_live_returns_404(client):
    """GET /health/live returns 404 — the route was removed (round-4
    standard: every component serves GET /health for liveness)."""
    r = client.get("/health/live")
    assert r.status_code == 404


def test_health_returns_alive(client):
    """GET /health returns 200 with status 'alive' and uptime_seconds."""
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "alive"
    assert "uptime_seconds" in data
    assert isinstance(data["uptime_seconds"], int)
    assert data["uptime_seconds"] >= 0


def test_health_ready_all_ok(client, monkeypatch):
    """GET /health/ready returns 200 with status 'ready' when both checks pass."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._health._build_read_client",
        lambda settings: _FakeLangfuseClient(),
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._health._langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: {"data": []},
    )

    r = client.get("/health/ready")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    names = {c["name"] for c in data["checks"]}
    assert names == {"database", "langfuse"}
    for c in data["checks"]:
        assert c["status"] == "ok"
        assert isinstance(c["latency_ms"], int)
        assert c["latency_ms"] >= 0


def test_health_ready_db_failure_returns_503(client, monkeypatch):
    """GET /health/ready returns 503 when the database check raises."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._health._build_read_client",
        lambda settings: _FakeLangfuseClient(),
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._health._langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: {"data": []},
    )

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._health.db",
        type(
            "BoomDB",
            (),
            {
                "get_engine": lambda *a, **kw: (_ for _ in ()).throw(
                    RuntimeError("simulated DB outage")
                )
            },
        )(),
    )

    r = client.get("/health/ready")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "not_ready"
    db_check = next(c for c in data["checks"] if c["name"] == "database")
    assert db_check["status"] == "error"


def test_health_ready_langfuse_unconfigured_is_skipped_not_503(client, monkeypatch):
    """GET /health/ready returns 200 with langfuse 'skipped' when
    _build_read_client returns None (Langfuse not configured)."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._health._build_read_client",
        lambda settings: None,
    )

    r = client.get("/health/ready")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ready"
    lf = next(c for c in data["checks"] if c["name"] == "langfuse")
    assert lf["status"] == "skipped"
    # Database should still be ok.
    db_check = next(c for c in data["checks"] if c["name"] == "database")
    assert db_check["status"] == "ok"


def test_health_ready_langfuse_error_returns_503(client, monkeypatch):
    """GET /health/ready returns 503 when _build_read_client returns a
    client (configured) but _langfuse_api_get returns None (unreachable)."""
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._health._build_read_client",
        lambda settings: _FakeLangfuseClient(),
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._health._langfuse_api_get",
        lambda settings, path, params=None, repo_config=None: None,
    )

    r = client.get("/health/ready")
    assert r.status_code == 503
    data = r.json()
    assert data["status"] == "not_ready"
    lf = next(c for c in data["checks"] if c["name"] == "langfuse")
    assert lf["status"] == "error"


# -- GET /metrics (Prometheus) -----------------------------------------


def test_metrics_endpoint_returns_valid_prometheus_format(client):
    """GET /metrics returns 200, text/plain Prometheus exposition format,
    and contains standard http_* metrics."""
    pytest.importorskip("prometheus_fastapi_instrumentator")

    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"] == "text/plain; version=1.0.0; charset=utf-8"
    assert "http_requests_total" in r.text
    assert "http_request_duration_seconds" in r.text


# -- GET /tickets board-poll cache (board_list_cache_ttl_seconds) -------


def test_board_list_cache_serves_snapshot_within_ttl(tmp_path, repos_registry):
    """With the cache enabled, repeated GET /tickets within the TTL serve a
    snapshot — a ticket created after the first poll is not visible until the
    cache expires/clears. (Field default is 0.0/off; production enables it.)"""
    from robotsix_mill.config import Settings
    from robotsix_mill.core import db
    from robotsix_mill.core.service import TicketService
    from robotsix_mill.runtime.routes import _tickets as tickets_routes

    db.reset_engine()
    s = Settings(
        data_dir=str(tmp_path),
        require_approval="false",
        board_list_cache_ttl_seconds=60.0,
    )
    db.init_db(s, board_id="test-board")
    svc = TicketService(s, board_id="test-board")
    tickets_routes._LIST_CACHE.clear()
    try:
        with TestClient(create_app(repos_registry, s, single_repo_id="test-repo")) as c:
            before_ids = {t["id"] for t in c.get("/tickets").json()}
            t = svc.create("cache probe")
            assert t.id not in before_ids
            # Within the TTL the cached snapshot is served — new ticket hidden.
            cached_ids = {x["id"] for x in c.get("/tickets").json()}
            assert t.id not in cached_ids
            # Clearing the cache forces a recompute — new ticket now visible.
            tickets_routes._LIST_CACHE.clear()
            fresh_ids = {x["id"] for x in c.get("/tickets").json()}
            assert t.id in fresh_ids
    finally:
        tickets_routes._LIST_CACHE.clear()
        db.reset_engine()


def test_board_list_cache_disabled_by_default(tmp_path, repos_registry):
    """With the default (ttl=0.0) the list is always fresh: a ticket created
    after the first poll appears immediately on the next poll."""
    from robotsix_mill.config import Settings
    from robotsix_mill.core import db
    from robotsix_mill.core.service import TicketService
    from robotsix_mill.runtime.routes import _tickets as tickets_routes

    db.reset_engine()
    s = Settings(data_dir=str(tmp_path), require_approval="false")
    assert s.board_list_cache_ttl_seconds == 0.0
    db.init_db(s, board_id="test-board")
    svc = TicketService(s, board_id="test-board")
    tickets_routes._LIST_CACHE.clear()
    try:
        with TestClient(create_app(repos_registry, s, single_repo_id="test-repo")) as c:
            c.get("/tickets")
            t = svc.create("fresh probe")
            ids = {x["id"] for x in c.get("/tickets").json()}
            assert t.id in ids
    finally:
        db.reset_engine()


# -- GET /chat-skill ----------------------------------------------------


def test_chat_skill_returns_markdown(client):
    """GET /chat-skill returns text/markdown with YAML frontmatter."""
    r = client.get("/chat-skill")
    assert r.status_code == 200
    content_type = r.headers["content-type"]
    assert "text/markdown" in content_type
    body = r.text
    assert body.startswith("---\n")
    assert "name: mill-board" in body
    assert "description:" in body
    assert "## mill-board — Chat Agent Skill" in body
    assert "GET /tickets" in body
    assert "POST /tickets/ingest" in body
    assert "robotsix-chat" in body
    assert "Safety rules" in body
