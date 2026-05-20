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
    assert '<div id="board">' in body
    assert '<div id="drawer">' in body
    # state labels live in the external JS; verify they're served there
    js = client.get("/static/board.js").text
    for s in ("draft", "awaiting_approval", "ready", "deliverable", "done", "blocked"):
        assert s in js
    assert "/tickets" in js  # board polls the JSON API


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


def test_get_tickets_includes_origin_session(client):
    """GET /tickets response includes origin_session (None for human-created tickets)."""
    client.post("/tickets", json={"title": "Origin check"})
    ts = client.get("/tickets").json()
    for t in ts:
        assert "origin_session" in t
        assert t["origin_session"] is None
        # origin_session_url is also present but None (no Langfuse config).
        assert "origin_session_url" in t


def test_get_tickets_includes_cost_usd(client, service, monkeypatch):
    """GET /tickets injects cost_usd read on-demand from the Langfuse
    session (not persisted) via langfuse_client.session_cost."""
    t = service.create("Cost API test")
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost",
        lambda settings, sid: 0.0420 if sid == t.id else 0.0,
    )

    ts = client.get("/tickets").json()
    found = [x for x in ts if x["id"] == t.id]
    assert len(found) == 1
    assert "cost_usd" in found[0]
    assert found[0]["cost_usd"] == pytest.approx(0.0420)


def test_board_renders_source_badge(client):
    """The board CSS includes source badge styling classes, and the
    HTML shell references the static CSS file."""
    r = client.get("/")
    body = r.text
    assert '<link rel="stylesheet" href="/static/board.css">' in body
    css = client.get("/static/board.css").text
    assert "src-badge" in css
    assert "src-user" in css
    assert "src-retrospect" in css


def test_board_renders_cost_snippet(client):
    """The board JS includes the JS snippet that renders cost on each
    card: $(t.cost_usd||0).toFixed(4), and the CSS has .cost class."""
    js = client.get("/static/board.js").text
    assert "cost_usd" in js   # JS references the field
    assert "toFixed(4)" in js  # 4 decimal places
    css = client.get("/static/board.css").text
    assert ".cost" in css      # CSS class for cost display


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
    # draft -> in_review is not a legal edge (drafts can't jump onto a PR).
    # NOTE: draft -> done IS legal — refine's dedup-discard path uses it so
    # the discarded draft still passes through retrospect.
    r = client.post(f"/tickets/{tid}/transition", json={"state": "in_review"})
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
    """Regression: a malformed template literal in board.js (a missing
    closing backtick on the Approve button) was a JS syntax error that
    wedged the whole board on 'loading…'. Guard the structural
    invariants."""
    from pathlib import Path

    import robotsix_mill.runtime.board_html

    js_path = (
        Path(robotsix_mill.runtime.board_html.__file__).parent
        / "static"
        / "board.js"
    )
    js = js_path.read_text()
    assert js.count("`") % 2 == 0, "unbalanced template-literal backticks"
    assert '</button>":' not in js  # the exact past defect
    assert '</button>`:' in js      # correctly-closed literal


def test_board_js_includes_origin_session_rendering(client):
    """The board JS includes origin_session and origin_session_url
    rendering logic for the ticket detail drawer."""
    js = client.get("/static/board.js").text
    assert "origin_session_url" in js
    assert "origin_session" in js
    assert "origin-link" in js


def test_board_css_includes_origin_link_style(client):
    """The board CSS includes the .origin-link style rule."""
    css = client.get("/static/board.css").text
    assert ".origin-link" in css


def test_origin_session_url_computed_when_config_set(service, settings):
    """enrich_ticket_read computes origin_session_url when all config
    ingredients are present."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("URL test", origin_session="sess-abc")
    settings.langfuse_base_url = "https://cloud.langfuse.com"
    settings.langfuse_project_id = "proj-xyz"

    tr = enrich_ticket_read(t, settings, service)
    assert tr.origin_session == "sess-abc"
    assert tr.origin_session_url == (
        "https://cloud.langfuse.com/project/proj-xyz/sessions/sess-abc"
    )


def test_origin_session_url_none_when_config_missing(service, settings):
    """enrich_ticket_read leaves origin_session_url None when any config
    ingredient is missing."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("No URL test", origin_session="sess-abc")
    # No langfuse_base_url or project_id set.
    tr = enrich_ticket_read(t, settings, service)
    assert tr.origin_session == "sess-abc"
    assert tr.origin_session_url is None


def test_board_html_references_static_assets(client):
    """GET / returns HTML that references the static CSS and JS files
    rather than embedding them inline."""
    body = client.get("/").text
    assert '<link rel="stylesheet" href="/static/board.css">' in body
    assert '<script src="/static/board.js"></script>' in body


def test_static_assets_served(client):
    """GET /static/board.css returns 200 text/css; GET /static/board.js
    returns 200 and contains key JS identifiers."""
    css = client.get("/static/board.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

    js = client.get("/static/board.js")
    assert js.status_code == 200
    assert "refresh" in js.text
    assert "open_" in js.text
    assert "newTicket" in js.text


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


def test_delete_ticket_endpoint(client, service):
    t = service.create("deletable")
    r = client.delete("/tickets/" + t.id)
    assert r.status_code == 204
    assert service.get(t.id) is None
    # second delete → 404
    assert client.delete("/tickets/" + t.id).status_code == 404
    assert client.delete("/tickets/nope").status_code == 404


def test_board_has_new_ticket_affordance(client):
    """The board exposes a user-facing 'create draft' control wired to
    POST /tickets (so a human can file a ticket from the UI)."""
    body = client.get("/").text
    assert "newTicket()" in body
    assert "+ New Ticket" in body
    # fetch("/tickets",{method:"POST" is in the external JS
    js = client.get("/static/board.js").text
    assert 'fetch("/tickets",{method:"POST"' in js


def test_post_tickets_creates_user_draft(client):
    """The control's backend: POST /tickets -> a DRAFT, source=user."""
    r = client.post("/tickets", json={"title": "From the board", "description": "idea"})
    assert r.status_code == 201
    d = r.json()
    assert d["state"] == "draft"
    assert d["source"] == "user"


# --- depends_on API ----------------------------------------------------

def test_create_ticket_with_depends_on(client):
    """POST /tickets accepts depends_on and the field is present in the response."""
    r = client.post("/tickets", json={
        "title": "Dep ticket API",
        "depends_on": '["ticket-aaa", "ticket-bbb"]',
    })
    assert r.status_code == 201
    data = r.json()
    assert data["depends_on"] == '["ticket-aaa", "ticket-bbb"]'
    assert "unmet_deps" in data


def test_get_ticket_includes_depends_on_and_unmet_deps(client):
    """GET /tickets/{id} includes depends_on and unmet_deps fields."""
    r = client.post("/tickets", json={
        "title": "With dep",
        "depends_on": '["some-other-ticket"]',
    })
    assert r.status_code == 201
    tid = r.json()["id"]

    got = client.get(f"/tickets/{tid}")
    assert got.status_code == 200
    data = got.json()
    assert "depends_on" in data
    assert data["depends_on"] == '["some-other-ticket"]'
    assert "unmet_deps" in data
    # The dep doesn't exist → treated satisfied → unmet_deps empty
    assert data["unmet_deps"] == []


def test_list_tickets_includes_depends_on_and_unmet_deps(client):
    """GET /tickets includes depends_on and unmet_deps for all tickets."""
    r = client.post("/tickets", json={
        "title": "List dep test",
        "depends_on": '["x", "y"]',
    })
    assert r.status_code == 201

    ts = client.get("/tickets").json()
    found = [t for t in ts if t["title"] == "List dep test"]
    assert len(found) == 1
    assert found[0]["depends_on"] == '["x", "y"]'
    assert "unmet_deps" in found[0]


def test_create_ticket_without_depends_on_has_none(client):
    """POST /tickets without depends_on → field is None."""
    r = client.post("/tickets", json={"title": "No dep"})
    assert r.status_code == 201
    data = r.json()
    assert data["depends_on"] is None
    assert data["unmet_deps"] == []


def test_list_tickets_include_closed_hides_only_closed_keeps_done(client, service):
    """include_closed=false must hide CLOSED but ALWAYS return DONE —
    DONE is the transient retrospect-in-flight window and needs to
    stay visible so the board can show retrospect work without the
    user toggling 'Show closed.'"""
    # Create via the service (not the API) to bypass maybe_enqueue —
    # the worker would otherwise refine these tickets and BLOCK them
    # on the missing API key, racing the transitions below.
    closed = service.create("C-closed")
    done = service.create("C-done")
    draft = service.create("C-draft")
    # Walk the two via legal edges: DRAFT -> DONE (refine's
    # dedup-discard route), DONE -> CLOSED (retrospect's edge).
    service.transition(closed.id, State.DONE)
    service.transition(closed.id, State.CLOSED)
    service.transition(done.id, State.DONE)

    # include_closed=true → everything visible.
    ids_all = {t["id"] for t in client.get("/tickets").json()}
    assert {closed.id, done.id, draft.id} <= ids_all

    # include_closed=false → CLOSED hidden, DONE + DRAFT still visible.
    ids = {t["id"] for t in client.get("/tickets?include_closed=false").json()}
    assert done.id in ids, "DONE must stay visible (retrospect-in-flight)"
    assert draft.id in ids
    assert closed.id not in ids, "CLOSED must be hidden by the toggle"


def test_board_js_includes_depends_on_rendering(client):
    """The board JS includes depends_on and unmet_deps rendering logic."""
    js = client.get("/static/board.js").text
    assert "depends_on" in js
    assert "unmet_deps" in js
    assert "⏳ waiting on" in js
