import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.states import State
from robotsix_mill.runtime.api import create_app


@pytest.fixture
def client(settings):
    # TestClient runs the lifespan: init_db, worker start/stop.
    with TestClient(create_app(settings)) as c:
        yield c


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_board_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "robotsix-mill" in body
    # the kanban columns are the pipeline states
    for s in ("draft", "awaiting_approval", "ready", "deliverable", "done", "blocked"):
        assert s in body
    assert "/tickets" in body  # board polls the JSON API


def test_create_and_get(client):
    r = client.post("/tickets", json={"title": "T", "description": "body"})
    assert r.status_code == 201
    tid = r.json()["id"]

    got = client.get(f"/tickets/{tid}")
    assert got.status_code == 200
    assert got.json()["title"] == "T"

    desc = client.get(f"/tickets/{tid}/description").json()
    assert desc["description"] == "body"

    assert tid in [t["id"] for t in client.get("/tickets").json()]


def test_create_ticket_source_is_user(client):
    """POST /tickets creates a ticket with source='user'."""
    r = client.post("/tickets", json={"title": "Source check"})
    assert r.status_code == 201
    data = r.json()
    assert data["source"] == "user"


def test_get_tickets_includes_source(client):
    """GET /tickets response includes source for each ticket."""
    client.post("/tickets", json={"title": "S1"})
    client.post("/tickets", json={"title": "S2"})
    ts = client.get("/tickets").json()
    assert len(ts) >= 2
    for t in ts:
        assert "source" in t
        assert t["source"] == "user"


def test_board_renders_source_badge(client):
    """The board HTML includes source badge styling and rendering."""
    r = client.get("/")
    body = r.text
    assert "src-badge" in body
    assert "src-user" in body
    assert "src-retrospect" in body


def test_get_missing_404(client):
    assert client.get("/tickets/nope").status_code == 404


def test_illegal_transition_409(client):
    tid = client.post("/tickets", json={"title": "T"}).json()["id"]
    r = client.post(f"/tickets/{tid}/transition", json={"state": "done"})
    assert r.status_code == 409


# --- approve endpoint tests ---


def test_approve_transitions_awaiting_approval_to_ready(client, service):
    """POST /tickets/{id}/approve moves awaiting_approval -> ready."""
    t = service.create("Approve me")
    service.transition(t.id, State.AWAITING_APPROVAL, note="refined")
    assert service.get(t.id).state is State.AWAITING_APPROVAL

    r = client.post(f"/tickets/{t.id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id
    assert data["state"] == State.READY


def test_approve_missing_ticket_404(client):
    """POST /tickets/{id}/approve with bogus id returns 404."""
    r = client.post("/tickets/nonexistent/approve")
    assert r.status_code == 404


def test_approve_wrong_state_409(client, service):
    """POST /tickets/{id}/approve on a non-awaiting_approval ticket returns 409."""
    t = service.create("Already ready")
    service.transition(t.id, State.READY, note="refined (autonomous)")

    r = client.post(f"/tickets/{t.id}/approve")
    assert r.status_code == 409


def test_approve_enqueues_implement(client, service):
    """After approve, the ticket is in ready (STAGE_FOR_STATE) so the
    worker picks it up. We verify the response is correct; the worker
    races to process it so we check the response, not the final state."""
    from robotsix_mill.core.states import STAGE_FOR_STATE

    t = service.create("Enqueue test")
    service.transition(t.id, State.AWAITING_APPROVAL, note="refined")

    r = client.post(f"/tickets/{t.id}/approve")
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == State.READY
    # READY is in STAGE_FOR_STATE -> implement should pick it up
    assert State.READY in STAGE_FOR_STATE


# --- cost_usd in API responses ---


def test_create_ticket_cost_usd_zero(client):
    """POST /tickets creates a ticket with cost_usd=0.0."""
    r = client.post("/tickets", json={"title": "Cost zero"})
    assert r.status_code == 201
    data = r.json()
    assert "cost_usd" in data
    assert data["cost_usd"] == 0.0


def test_get_tickets_includes_cost_usd(client):
    """GET /tickets response includes cost_usd for each ticket."""
    client.post("/tickets", json={"title": "C1"})
    client.post("/tickets", json={"title": "C2"})
    ts = client.get("/tickets").json()
    assert len(ts) >= 2
    for t in ts:
        assert "cost_usd" in t
        assert t["cost_usd"] == 0.0


def test_get_ticket_returns_cost_usd_with_known_value(client, service):
    """GET /tickets/{id} returns cost_usd matching what was set."""
    t = service.create("Known cost")
    service.add_cost(t.id, 0.0943)

    r = client.get(f"/tickets/{t.id}")
    assert r.status_code == 200
    data = r.json()
    assert data["cost_usd"] == 0.0943


# --- board HTML cost rendering ---


def test_board_has_cost_rendering_code(client):
    """The board HTML source includes the cost rendering function and
    CSS class (cost values are populated by client-side JS from the
    /tickets JSON endpoint, not server-side)."""
    r = client.get("/")
    body = r.text
    # The fmtCost function formats cost with $ prefix and 4 decimal places
    assert "fmtCost" in body
    assert ".cost" in body
    assert "cost_usd" in body  # JS reads this field from JSON
    # CSS class for the cost span
    assert "class=\"cost\"" in body


def test_board_rendering_no_langfuse_calls(client):
    """Board rendering should NOT call Langfuse (no external HTTP requests).
    The board is pure static HTML + API JSON; the worker handles tracing.
    Since tests are hermetic, any external request would fail — we just
    verify the board endpoint succeeds and doesn't reference Langfuse."""
    r = client.get("/")
    assert r.status_code == 200
    # The board HTML does not contain any Langfuse references
    assert "langfuse" not in r.text.lower()
