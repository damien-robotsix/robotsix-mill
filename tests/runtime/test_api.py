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
    for s in ("draft", "human_issue_approval", "ready", "deliverable", "done", "blocked"):
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


def test_get_ticket_detail_includes_cost_usd(client, service, monkeypatch):
    """GET /tickets/{id} (the per-ticket detail) injects cost_usd
    on-demand from the Langfuse session via session_cost. The list
    endpoint is intentionally cache-only (see test below) — only the
    drawer view does the live lookup."""
    t = service.create("Cost API test")
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost",
        lambda settings, sid: 0.0420 if sid == t.id else 0.0,
    )

    r = client.get(f"/tickets/{t.id}").json()
    assert r["id"] == t.id
    assert r["cost_usd"] == pytest.approx(0.0420)


def test_get_tickets_list_is_cache_only_for_cost(client, service, monkeypatch):
    """GET /tickets (the polled list) must NEVER call the blocking
    Langfuse session_cost — that would cost N serial HTTP roundtrips
    on cold cache and stall the response past the board's 5s poll
    interval. Cost comes from session_cost_cached (no network), so
    the list value is 0.0 for an unseeded cache, regardless of what
    session_cost is monkeypatched to return."""
    t = service.create("Cache-only test")
    called = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.session_cost",
        lambda settings, sid: called.append(sid) or 0.999,
    )

    ts = client.get("/tickets").json()
    found = [x for x in ts if x["id"] == t.id]
    assert len(found) == 1
    assert found[0]["cost_usd"] == 0.0, (
        "list endpoint must use the non-blocking cached path"
    )
    assert called == [], (
        f"blocking session_cost must not be called by /tickets list; got {called}"
    )


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
    # draft -> human_mr_approval is not a legal edge (drafts can't jump onto a PR).
    # NOTE: draft -> done IS legal — refine's dedup-discard path uses it so
    # the discarded draft still passes through retrospect.
    r = client.post(f"/tickets/{tid}/transition", json={"state": "human_mr_approval"})
    assert r.status_code == 409


# --- approve endpoint tests ---


def test_approve_transitions_human_issue_approval_to_ready(client, service):
    """POST /tickets/{id}/approve moves human_issue_approval -> ready."""
    t = service.create("Approve me")
    service.transition(t.id, State.HUMAN_ISSUE_APPROVAL, note="refined")
    assert service.get(t.id).state is State.HUMAN_ISSUE_APPROVAL

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
    """POST /tickets/{id}/approve on a non-human_issue_approval ticket returns 409."""
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
    service.transition(t.id, State.HUMAN_ISSUE_APPROVAL, note="refined")

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
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="PR opened")
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


def test_agent_check_endpoint_is_fire_and_forget(client, monkeypatch):
    """POST /agent-check returns 202 immediately and runs the
    agent-check agent in the background — same fire-and-forget
    contract as /audit, /health-check, /trace-health."""
    import threading

    from robotsix_mill import agent_check_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_agent_check():
        ran.set()
        release.wait(5)
        return _R()

    monkeypatch.setattr(
        agent_check_runner, "run_agent_check_pass", slow_agent_check
    )

    r = client.post("/agent-check")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)
    release.set()


def test_deep_review_session_id_format(client, monkeypatch):
    """POST /traces/{trace_id}/deep-review uses make_session_id("deep-review")
    as the ticket_id for the root span (not the source trace_id), and
    passes the source trace_id as extra_attributes."""
    import contextlib
    import time

    from robotsix_mill.runtime import tracing
    from robotsix_mill.agents import trace_inspector
    from robotsix_mill.config import Settings

    # The default test settings don't have Langfuse keys, so the
    # endpoint's tracing_enabled check would short-circuit.
    monkeypatch.setattr(
        Settings, "tracing_enabled", property(lambda self: True),
    )

    seen = {}

    @contextlib.contextmanager
    def fake_root(ticket_id, stage_name=None, extra_attributes=None):
        seen["ticket_id"] = ticket_id
        seen["stage_name"] = stage_name
        seen["extra_attributes"] = extra_attributes
        yield

    monkeypatch.setattr(tracing, "start_ticket_root_span", fake_root)

    # Provide a fake trace detail so the endpoint doesn't 404.
    monkeypatch.setattr(
        "robotsix_mill.langfuse_client.fetch_trace_detail",
        lambda s, tid: {"id": tid, "name": "test-trace"},
    )
    # Make the inspector return success.
    monkeypatch.setattr(
        trace_inspector, "run_trace_inspector",
        lambda **kw: trace_inspector.TraceInspectorResult(
            updated_memory="", findings=[], tool_errors=[],
            agent_limitations=[], optimizations=[], error=None,
        ),
    )

    trace_id = "some-source-trace-abc123"
    r = client.post(f"/traces/{trace_id}/deep-review")
    assert r.status_code == 202

    # Wait briefly for the background thread.
    deadline = time.monotonic() + 2
    while "ticket_id" not in seen and time.monotonic() < deadline:
        time.sleep(0.02)

    assert "ticket_id" in seen, "start_ticket_root_span was never called"
    assert seen["ticket_id"].startswith("deep-review-"), \
        f"ticket_id={seen['ticket_id']!r} should start with 'deep-review-'"
    assert trace_id not in seen["ticket_id"], \
        f"source trace_id must not appear in session id: {seen['ticket_id']!r}"
    assert seen["stage_name"] == "deep-review"
    assert seen["extra_attributes"] == {"source_trace_id": trace_id}


def test_board_html_includes_agent_check_button(client):
    """The board exposes an 'Agent Check' button wired to
    runAgentCheck() in the JS. Without it the user can't see the
    agent-check feature exists, and only the CLI is discoverable."""
    body = client.get("/").text
    assert "Agent Check" in body
    assert "runAgentCheck()" in body
    js = client.get("/static/board.js").text
    assert "runAgentCheck" in js
    assert 'jpost("/agent-check")' in js


def test_board_has_last_reviews_panel(client):
    """The board JS includes a 'Last reviews' affordance wired to
    GET /deep-review so users can replay past deep-review findings."""
    js = client.get("/static/board.js").text
    assert "Last reviews" in js, (
        "'Last reviews' literal must appear in board.js — "
        "the UI panel for replaying past deep-review results"
    )
    assert 'renderLastReviewsList' in js
    assert 'viewStoredReview' in js
    assert 'jget("/deep-review")' in js or 'jget("/deep-review"' in js, (
        "board.js must call GET /deep-review to fetch stored reviews"
    )


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
    # POST /tickets is in the external JS (now via the XHR helper,
    # not fetch — fetch is wrapped by SES/extensions and unreliable).
    js = client.get("/static/board.js").text
    assert 'jpost("/tickets"' in js


def test_board_has_new_inquiry_affordance(client):
    """The board exposes a '+ Ask' button wired to POST /tickets with kind='inquiry'.

    Regression guard: the inquiry backend landed but the button was
    forgotten (same as the comment-UI gap). This assertion prevents recurrence.
    """
    body = client.get("/").text
    assert "newInquiry()" in body
    assert "+ Ask" in body
    js = client.get("/static/board.js").text
    assert "newInquiry" in js
    # The only thing that distinguishes inquiry creation from task creation:
    assert 'kind:"inquiry"' in js, (
        "newInquiry() must POST kind='inquiry', not the default 'task' — "
        "without this the button silently creates tasks instead of inquiries"
    )


def test_post_tickets_creates_user_draft(client):
    """The control's backend: POST /tickets -> a DRAFT, source=user."""
    r = client.post("/tickets", json={"title": "From the board", "description": "idea"})
    assert r.status_code == 201
    d = r.json()
    assert d["state"] == "draft"
    assert d["source"] == "user"


def test_post_tickets_with_kind_inquiry_creates_asked_inquiry(client):
    """POST /tickets with kind='inquiry' creates an inquiry in ASKED state.

    This is the backend path the '+ Ask' button drives.
    """
    r = client.post(
        "/tickets",
        json={"title": "Why does X happen?", "description": "context", "kind": "inquiry"},
    )
    assert r.status_code == 201
    d = r.json()
    assert d["state"] == "asked"  # inquiries start in ASKED, not DRAFT
    assert d["kind"] == "inquiry"
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


def test_create_inquiry_with_depends_on_is_rejected(client):
    """POST /tickets with kind='inquiry' and depends_on raises 400.

    Inquiries are standalone Q&A — they don't wait on other tickets.
    """
    r = client.post(
        "/tickets",
        json={
            "title": "Inquiry with dep",
            "kind": "inquiry",
            "depends_on": '["ticket-abc"]',
        },
    )
    assert r.status_code in (400, 422), (
        "inquiries must reject depends_on — they are standalone Q&A"
    )


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
    # Walk via legal edges: DRAFT -> DONE (refine's dedup-discard route),
    # DONE -> CLOSED (retrospect's edge).
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


def test_get_retrospect_returns_artifact_or_empty(client, service, tmp_path):
    """GET /tickets/{id}/retrospect returns the retrospect.md artifact,
    or {'retrospect': ''} when no artifact exists yet."""
    t = service.create("Retrospect read")
    # No artifact yet → empty string.
    r = client.get(f"/tickets/{t.id}/retrospect").json()
    assert r == {"retrospect": ""}

    # Write an artifact and re-read.
    ws = service.workspace(t)
    (ws.artifacts_dir / "retrospect.md").write_text(
        "# Retrospect\nlangfuse: yes\n\nMeaningful analysis.\n",
        encoding="utf-8",
    )
    r2 = client.get(f"/tickets/{t.id}/retrospect").json()
    assert "Meaningful analysis" in r2["retrospect"]

    # 404 for an unknown ticket.
    assert client.get("/tickets/no-such/retrospect").status_code == 404


def test_board_js_includes_depends_on_rendering(client):
    """The board JS includes depends_on and unmet_deps rendering logic."""
    js = client.get("/static/board.js").text
    assert "depends_on" in js
    assert "unmet_deps" in js
    assert "⏳ waiting on" in js


# -- DeepReviewStore unit tests -----------------------------------------


def test_deep_review_store_round_trip(tmp_path):
    from robotsix_mill.runtime.deep_review_store import DeepReviewStore

    store = DeepReviewStore(tmp_path / "reviews.json")
    store.put("a", {"status": "ok", "trace_id": "a"})
    store.put("b", {"status": "error", "trace_id": "b", "error": "boom"})
    store.put("c", {"status": "ok", "trace_id": "c"})

    entries = store.list_all()
    assert len(entries) == 3
    # Newest first — "c" should be first, then "b", then "a".
    assert entries[0]["trace_id"] == "c"
    assert entries[1]["trace_id"] == "b"
    assert entries[2]["trace_id"] == "a"

    # All entries have finished_at.
    for e in entries:
        assert "finished_at" in e

    # get by trace_id
    assert store.get("a")["trace_id"] == "a"
    assert store.get("b")["status"] == "error"
    assert store.get("c")["status"] == "ok"
    assert store.get("nonexistent") is None


def test_deep_review_store_cap_enforcement(tmp_path):
    from robotsix_mill.runtime.deep_review_store import DeepReviewStore

    store = DeepReviewStore(tmp_path / "reviews.json")
    for i in range(25):
        store.put(f"trace-{i:03d}", {"status": "ok", "trace_id": f"trace-{i:03d}", "idx": i})

    entries = store.list_all()
    assert len(entries) == 20
    # The 20 newest — these are the ones with highest "idx" (since put later).
    kept_ids = {e["trace_id"] for e in entries}
    for i in range(5):
        assert f"trace-{i:03d}" not in kept_ids  # oldest 5 evicted
    for i in range(5, 25):
        assert f"trace-{i:03d}" in kept_ids


def test_deep_review_store_atomic_write_integrity(tmp_path):
    """After each put(), the file on disk is always valid JSON."""
    import json
    from robotsix_mill.runtime.deep_review_store import DeepReviewStore

    store = DeepReviewStore(tmp_path / "reviews.json")
    for i in range(5):
        store.put(f"trace-{i}", {"status": "ok", "trace_id": f"trace-{i}"})
        raw = (tmp_path / "reviews.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        assert isinstance(data, list)
        # No tmp file left behind.
        assert not (tmp_path / "reviews.json.tmp").exists()


def test_deep_review_store_corrupt_file_recovery(tmp_path):
    from robotsix_mill.runtime.deep_review_store import DeepReviewStore

    # Write invalid JSON to the file.
    (tmp_path / "reviews.json").write_text("this is not json", encoding="utf-8")

    store = DeepReviewStore(tmp_path / "reviews.json")
    # Should not crash, should return empty.
    assert store.list_all() == []
    assert store.get("anything") is None

    # A put() should replace the corrupt file with valid JSON.
    store.put("recovered", {"status": "ok", "trace_id": "recovered"})
    entries = store.list_all()
    assert len(entries) == 1
    assert entries[0]["trace_id"] == "recovered"

    # File on disk should now be valid JSON.
    import json
    raw = (tmp_path / "reviews.json").read_text(encoding="utf-8")
    assert json.loads(raw) == entries


def test_deep_review_store_overwrite(tmp_path):
    from robotsix_mill.runtime.deep_review_store import DeepReviewStore

    store = DeepReviewStore(tmp_path / "reviews.json")
    store.put("x", {"status": "ok", "trace_id": "x", "msg": "first"})
    store.put("x", {"status": "error", "trace_id": "x", "msg": "second"})

    entries = store.list_all()
    assert len(entries) == 1
    assert entries[0]["msg"] == "second"
    assert entries[0]["status"] == "error"


# -- Deep review API integration tests ---------------------------------


def test_list_deep_reviews_returns_array(client):
    """GET /deep-review returns a JSON array, 200."""
    r = client.get("/deep-review")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_list_deep_reviews_empty(client):
    """GET /deep-review on a fresh store returns []."""
    assert client.get("/deep-review").json() == []


def test_list_deep_reviews_ordering(client):
    """GET /deep-review returns entries newest-first."""
    from robotsix_mill.runtime.deep_review_store import DeepReviewStore

    store = DeepReviewStore(client.app.state.settings.data_dir / "deep_review_results.json")
    store.put("a", {"status": "ok", "trace_id": "a", "source_trace_name": "test"})
    store.put("b", {"status": "ok", "trace_id": "b", "source_trace_name": "test"})
    store.put("c", {"status": "ok", "trace_id": "c", "source_trace_name": "test"})

    r = client.get("/deep-review")
    entries = r.json()
    assert len(entries) == 3
    assert entries[0]["trace_id"] == "c"
    assert entries[1]["trace_id"] == "b"
    assert entries[2]["trace_id"] == "a"


def test_get_deep_review_falls_back_to_store(client):
    """GET /deep-review/{trace_id} returns 200 for a store-only entry."""
    from robotsix_mill.runtime.deep_review_store import DeepReviewStore

    store = DeepReviewStore(client.app.state.settings.data_dir / "deep_review_results.json")
    store.put("stored-trace", {
        "status": "ok",
        "trace_id": "stored-trace",
        "source_trace_name": "test",
        "tool_errors": [],
        "agent_limitations": [],
        "optimizations": [],
        "error": "",
        "findings": [],
    })

    # Entry is NOT in in-memory results.
    assert "stored-trace" not in client.app.state.deep_review_results

    r = client.get("/deep-review/stored-trace")
    assert r.status_code == 200
    assert r.json()["trace_id"] == "stored-trace"
    assert r.json()["status"] == "ok"


def test_get_deep_review_prefers_in_memory(client):
    """GET /deep-review/{trace_id} prefers in-memory over store."""
    from robotsix_mill.runtime.deep_review_store import DeepReviewStore

    store = DeepReviewStore(client.app.state.settings.data_dir / "deep_review_results.json")
    store.put("dual-trace", {
        "status": "ok", "trace_id": "dual-trace",
        "source_trace_name": "store_version",
        "tool_errors": [], "agent_limitations": [], "optimizations": [],
        "error": "", "findings": [],
    })

    # Put a different version in memory.
    client.app.state.deep_review_results["dual-trace"] = {
        "status": "error",
        "trace_id": "dual-trace",
        "error": "in-memory version",
        "findings": [],
    }

    r = client.get("/deep-review/dual-trace")
    assert r.status_code == 200
    assert r.json()["status"] == "error"
    assert r.json()["error"] == "in-memory version"


def test_get_deep_review_404_when_not_found(client):
    """GET /deep-review/{trace_id} returns 404 when not in memory or store."""
    r = client.get("/deep-review/no-such-trace")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Epic API tests
# ---------------------------------------------------------------------------

def test_create_epic_via_api(client):
    """POST /epics returns 201 with state='epic_open', kind='epic'."""
    r = client.post("/epics", json={"title": "My Epic", "description": "Big picture"})
    assert r.status_code == 201
    data = r.json()
    assert data["state"] == "epic_open"
    assert data["kind"] == "epic"


def test_create_ticket_with_parent(client, service):
    """POST /tickets with parent_id set links child to epic."""
    epic = service.create("Epic", kind="epic")
    r = client.post("/tickets", json={
        "title": "Child Task",
        "description": "detail",
        "parent_id": epic.id,
    })
    assert r.status_code == 201
    data = r.json()
    assert data["parent_id"] == epic.id


def test_list_children_endpoint(client, service):
    """GET /tickets/{epic_id}/children returns children."""
    epic = service.create("Epic", kind="epic")
    c1 = service.create("Child 1", kind="task", parent_id=epic.id)
    c2 = service.create("Child 2", kind="task", parent_id=epic.id)

    r = client.get(f"/tickets/{epic.id}/children")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    child_ids = {c["id"] for c in data}
    assert child_ids == {c1.id, c2.id}


# --- generate-children endpoint tests ---


def test_generate_children_404_nonexistent(client):
    """POST /tickets/nonexistent/generate-children returns 404."""
    r = client.post("/tickets/nonexistent/generate-children")
    assert r.status_code == 404


def test_generate_children_400_non_epic(client, service):
    """POST /tickets/{id}/generate-children on a task returns 400."""
    t = service.create("Not an epic", kind="task")
    r = client.post(f"/tickets/{t.id}/generate-children")
    assert r.status_code == 400
    assert "ticket is not an epic" in r.json()["detail"]


def test_generate_children_202_fire_and_forget(client, service, monkeypatch):
    """POST /tickets/{id}/generate-children returns 202 immediately and
    runs the agent in the background — the HTTP response must not
    block on the LLM call."""
    import threading

    epic = service.create("Fire and forget epic", kind="epic")

    ran = threading.Event()
    release = threading.Event()

    def slow_agent(*, settings, epic_title, epic_description):
        ran.set()
        release.wait(5)
        return type(
            "FakeResult",
            (),
            {"child_titles": [], "child_bodies": []},
        )()

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        slow_agent,
    )

    r = client.post(f"/tickets/{epic.id}/generate-children")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5), "agent did not start in background"
    release.set()  # let the daemon thread finish


def test_generate_children_creates_children(client, service, monkeypatch):
    """POST /tickets/{id}/generate-children creates child tickets with
    the titles and bodies returned by the agent."""
    import threading

    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult

    epic = service.create("Break me down", kind="epic")

    # Signal when the background thread has created both children.
    children_created = threading.Event()
    child_count = [0]
    app_svc = client.app.state.service
    orig_create = app_svc.create

    def tracking_create(title, **kwargs):
        result = orig_create(title, **kwargs)
        child_count[0] += 1
        if child_count[0] >= 2:
            children_created.set()
        return result

    monkeypatch.setattr(app_svc, "create", tracking_create)
    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        lambda **kw: EpicBreakdownResult(
            child_titles=["Child A", "Child B"],
            child_bodies=["Body A", "Body B"],
        ),
    )

    r = client.post(f"/tickets/{epic.id}/generate-children")
    assert r.status_code == 202

    # Wait for the background thread to finish creating children.
    assert children_created.wait(5), "children were not created in time"

    children = client.get(f"/tickets/{epic.id}/children").json()
    assert len(children) == 2, f"expected 2 children, got {len(children)}"
    child_titles = {c["title"] for c in children}
    assert child_titles == {"Child A", "Child B"}
