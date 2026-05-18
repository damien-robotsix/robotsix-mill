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


def test_get_tickets_includes_cost_usd(client, service):
    """GET /tickets response includes cost_usd with correct value."""
    t = service.create("Cost API test")
    service.set_cost(t.id, 0.0420)

    ts = client.get("/tickets").json()
    # Find our ticket in the list
    found = [x for x in ts if x["id"] == t.id]
    assert len(found) == 1
    assert "cost_usd" in found[0]
    assert found[0]["cost_usd"] == pytest.approx(0.0420)


def test_board_renders_source_badge(client):
    """The board HTML includes source badge styling and rendering."""
    r = client.get("/")
    body = r.text
    assert "src-badge" in body
    assert "src-user" in body
    assert "src-retrospect" in body


def test_board_renders_cost_snippet(client):
    """The board HTML includes the JS snippet that renders cost on
    each card: $(t.cost_usd||0).toFixed(4)."""
    r = client.get("/")
    body = r.text
    assert "cost_usd" in body  # JS references the field
    assert ".cost" in body     # CSS class for cost display
    assert "toFixed(4)" in body  # 4 decimal places


def test_board_no_langfuse_calls(client, monkeypatch):
    """Board rendering makes zero HTTP requests (so zero Langfuse API
    calls). Monkeypatch httpx.Client to guarantee it."""
    import httpx

    captured = []

    class NoNetworkClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            captured.append("Client()")
            raise AssertionError(
                "Board rendering must not make HTTP requests"
            )

    monkeypatch.setattr(httpx, "Client", NoNetworkClient)

    # Must not raise — the board is a static HTML string, no HTTP.
    r = client.get("/")
    assert r.status_code == 200
    assert "robotsix-mill" in r.text
    assert len(captured) == 0, "httpx.Client was instantiated during board rendering"


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


# --- resume-blocked endpoint tests ---


def test_resume_blocked_success(client, service):
    """POST /tickets/{id}/resume-blocked resumes BLOCKED → blocked_from."""
    t = service.create("Resume via API")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    assert service.get(t.id).state is State.BLOCKED
    assert service.get(t.id).blocked_from == State.READY.value

    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id
    assert data["state"] == State.READY


def test_resume_blocked_from_done(client, service):
    """POST /tickets/{id}/resume-blocked resumes BLOCKED (blocked from DONE) → DONE."""
    t = service.create("Retrospect resume via API")
    service.transition(t.id, State.READY, note="refined")
    service.transition(t.id, State.DELIVERABLE, note="implemented")
    service.transition(t.id, State.IN_REVIEW, note="PR opened")
    service.transition(t.id, State.DONE, note="merged")
    service.transition(t.id, State.BLOCKED, note="retrospect failed")

    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == State.DONE

    # Can then complete retrospect → CLOSED
    service.transition(t.id, State.CLOSED)
    assert service.get(t.id).state is State.CLOSED


def test_resume_blocked_missing_ticket_404(client):
    """POST /tickets/{id}/resume-blocked with bogus id returns 404."""
    r = client.post("/tickets/nonexistent/resume-blocked")
    assert r.status_code == 404


def test_resume_blocked_wrong_state_409(client, service):
    """POST /tickets/{id}/resume-blocked on non-BLOCKED ticket returns 409."""
    t = service.create("Not blocked")
    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 409


# --- BLOCKED manual override via transition endpoint ---


def test_blocked_override_to_ready_via_api(client, service):
    """POST /tickets/{id}/transition to READY overrides BLOCKED → READY.

    The API response is verified; the worker may race to process the
    ticket after enqueue (implement stage), so we don't assert final
    DB state here.  Service-level tests cover blocked_from clearing.
    """
    t = service.create("Override to ready via API")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    assert service.get(t.id).blocked_from == State.READY.value

    r = client.post(
        f"/tickets/{t.id}/transition",
        json={"state": State.READY.value, "note": "manual override"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == State.READY


def test_blocked_override_to_draft_via_api(client, service):
    """POST /tickets/{id}/transition to DRAFT overrides BLOCKED → DRAFT.

    The API response is verified; the worker may race to process the
    ticket after enqueue (refine stage), so we don't assert final
    DB state here.  Service-level tests cover blocked_from clearing.
    """
    t = service.create("Override to draft via API")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck")
    assert service.get(t.id).blocked_from == State.READY.value

    r = client.post(
        f"/tickets/{t.id}/transition",
        json={"state": State.DRAFT.value, "note": "manual override to draft"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == State.DRAFT


def test_board_script_is_well_formed():
    """Regression: a malformed template literal in _BOARD_HTML (a
    missing closing backtick on the Approve button) was a JS syntax
    error that wedged the whole board on 'loading…'. Guard the
    structural invariants."""
    import re

    from robotsix_mill.runtime.api import _BOARD_HTML

    js = re.search(r"<script>(.*?)</script>", _BOARD_HTML, re.S).group(1)
    assert js.count("`") % 2 == 0, "unbalanced template-literal backticks"
    assert '</button>":' not in _BOARD_HTML  # the exact past defect
    assert '</button>`:' in _BOARD_HTML      # correctly-closed literal


def test_audit_endpoint_is_fire_and_forget(client, monkeypatch):
    """Regression: POST /audit ran the LLM agent synchronously, so the
    browser fetch hung for minutes and dropped ('NetworkError'). It
    must return 202 immediately and run the audit in the background."""
    import threading

    from robotsix_mill import audit_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_audit():
        ran.set()
        release.wait(5)  # simulate a minutes-long run
        return _R()

    monkeypatch.setattr(audit_runner, "run_audit_pass", slow_audit)

    r = client.post("/audit")  # must NOT block on slow_audit
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)         # audit really started in the background
    release.set()              # let the daemon thread finish


def test_setup_logging_surfaces_app_logs_idempotently(capsys):
    """Regression: robotsix_mill.* logs were dropped (no handler under
    uvicorn), masking the silently-failing /audit thread. setup_logging
    must attach exactly one stdout handler at INFO, idempotently."""
    import logging

    from robotsix_mill.runtime.api import setup_logging

    root = logging.getLogger("robotsix_mill")
    root.handlers = [h for h in root.handlers if not getattr(h, "_mill", False)]

    setup_logging()
    setup_logging()  # idempotent — second call must not add another
    mill = [h for h in root.handlers if getattr(h, "_mill", False)]
    assert len(mill) == 1
    assert root.level == logging.INFO

    logging.getLogger("robotsix_mill.audit").info("audit pass starting xyz")
    assert "audit pass starting xyz" in capsys.readouterr().out
