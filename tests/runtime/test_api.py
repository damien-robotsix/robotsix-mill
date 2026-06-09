import json
import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.states import State
from robotsix_mill.core.models import SourceKind
from robotsix_mill.runtime.api import create_app


@pytest.fixture
def client(settings, repos_registry):
    # TestClient runs the lifespan: init_db, worker start/stop.
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_gates(client, settings):
    """GET /gates returns the four pipeline gate flags from the live Settings."""
    r = client.get("/gates")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "auto_approve": settings.auto_approve_enabled,
        "review": settings.review_enabled,
        "auto_merge": settings.auto_merge_enabled,
        "require_approval": settings.require_approval,
    }


def test_board_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "robotsix-mill" in body
    assert "cdn.jsdelivr.net/npm/marked" in body
    assert '<div id="board">' in body
    assert '<div id="drawer">' in body
    assert '<span id="gates">' in body  # gate pills placeholder
    # robotsix-board config script placeholder is present; when
    # robotsix-board is installed it will be replaced by
    # render_config_script() output.
    assert "{CONFIG_SCRIPT}" not in body  # replaced at request time
    # Mill-specific JS is linked
    assert "/static/mill/board-mill.js" in body
    assert "/static/mill/board-mill.css" in body
    # robotsix-board static assets are linked
    assert "/static/board.js" in body
    assert "/static/board.css" in body

    # State labels now live in the mill-specific overlay JS, not the
    # shared robotsix-board library.
    js = client.get("/static/mill/board-mill.js").text
    for s in ("draft", "human_issue_approval", "done"):
        assert s in js
    assert "/tickets" in js  # board polls for card data


def test_board_config_script_references_board_cards(client):
    """When robotsix-board is installed, the board config script contains
    the refresh_url pointing to /board/cards."""
    body = client.get("/").text
    # robotsix-board may or may not be installed; if it is, the config
    # script includes the refresh URL.
    if "board-config" in body:
        assert "/board/cards" in body


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
    assert data["source"] == SourceKind.USER


def test_get_tickets_includes_source(client):
    """GET /tickets response includes source for each ticket."""
    client.post("/tickets", json={"title": "S1"})
    client.post("/tickets", json={"title": "S2"})
    ts = client.get("/tickets").json()
    assert len(ts) >= 2
    for t in ts:
        assert "source" in t
        assert t["source"] == SourceKind.USER


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
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: 0.0420 if sid == t.id else 0.0,
    )

    r = client.get(f"/tickets/{t.id}").json()
    assert r["id"] == t.id
    assert r["cost_usd"] == pytest.approx(0.0420)


def test_get_tickets_list_is_cache_only_for_cost(client, service, monkeypatch):
    """GET /tickets (the polled list) builds its RESPONSE cache-only — never
    blocking on Langfuse session_cost during the request (that would cost N
    serial HTTP roundtrips on cold cache and stall the board poll). The cost
    comes from session_cost_cached (no network), so it's 0.0 for an unseeded
    cache. Warming then happens in a BACKGROUND task after the response is
    sent (replaces the old cost_warmer daemon)."""
    t = service.create("Cache-only test")
    called = []
    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: called.append(sid) or 0.999,
    )

    ts = client.get("/tickets").json()
    found = [x for x in ts if x["id"] == t.id]
    assert len(found) == 1
    # The RESPONSE is cache-only: 0.0 even though session_cost returns 0.999.
    assert found[0]["cost_usd"] == 0.0, (
        "list endpoint must build the response from the non-blocking cached path"
    )
    # The background warm scheduled a refresh for the returned ticket (it runs
    # after the response — TestClient executes background tasks before
    # returning), so the next poll shows the real value.
    assert t.id in called


def test_board_renders_source_badge(client):
    """The mill-specific overlay CSS includes source badge styling classes,
    and the HTML shell references both the shared and mill CSS files."""
    r = client.get("/")
    body = r.text
    assert '<link rel="stylesheet" href="/static/board.css">' in body
    assert '<link rel="stylesheet" href="/static/mill/board-mill.css">' in body
    css = client.get("/static/mill/board-mill.css").text
    assert "src-badge" in css
    assert "src-user" in css
    assert "src-retrospect" in css
    assert "src-survey" in css
    js = client.get("/static/mill/board-mill.js").text
    assert '"survey"' in js  # mapped in srcClass()


def test_board_renders_cost_snippet(client):
    """The mill overlay JS includes the JS snippet that renders cost on each
    card: $(t.cost_usd||0).toFixed(4), and the overlay CSS has .cost class."""
    js = client.get("/static/mill/board-mill.js").text
    assert "cost_usd" in js  # JS references the field
    assert "toFixed(4)" in js  # 4 decimal places
    css = client.get("/static/mill/board-mill.css").text
    assert ".cost" in css  # CSS class for cost display


def test_board_renders_gate_pill_wiring(client):
    """The mill overlay JS includes gate-fetching and pill-rendering logic,
    and the overlay CSS includes the gate-pill / gate-on / gate-off classes."""
    js = client.get("/static/mill/board-mill.js").text
    assert '"/gates"' in js
    assert "fetchGates" in js
    assert "gate-pill" in js
    assert "gate-on" in js
    assert "gate-off" in js
    # All four labels must appear.
    for label in ("auto-approve", "review", "auto-merge", "require-approval"):
        assert label in js, f"gate label '{label}' missing from board-mill.js"
    # The YAML paths (from the tooltip) should also appear.
    assert "gates.auto_approve_enabled" in js
    assert "gates.review_enabled" in js
    assert "gates.auto_merge_enabled" in js
    assert "gates.require_approval" in js
    css = client.get("/static/mill/board-mill.css").text
    assert ".gate-pill" in css
    assert ".gate-on" in css
    assert ".gate-off" in css


def test_board_no_langfuse_calls(client, monkeypatch):
    """Board rendering makes zero HTTP requests (so zero Langfuse API
    calls). Monkeypatch httpx.Client to guarantee it."""
    import httpx

    captured = []

    class NoNetworkClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            captured.append("Client()")
            raise AssertionError("Board rendering must not make HTTP requests")

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


# --- request-changes endpoint tests ---


def test_request_changes_empty_body_returns_200(client, service):
    """POST /tickets/{id}/request-changes with {"body": ""} returns 200, no comment created."""
    t = service.create("RC test")
    service.transition(t.id, State.HUMAN_ISSUE_APPROVAL, note="refined")

    r = client.post(f"/tickets/{t.id}/request-changes", json={"body": ""})
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    data = r.json()
    assert data["comment"] is None
    assert data["ticket"]["state"] == "draft"

    comments = service.list_comments(t.id)
    assert len(comments) == 0


def test_request_changes_nonempty_body_creates_comment(client, service):
    """POST /tickets/{id}/request-changes with a body creates a comment and transitions."""
    t = service.create("RC test")
    service.transition(t.id, State.HUMAN_ISSUE_APPROVAL, note="refined")

    r = client.post(f"/tickets/{t.id}/request-changes", json={"body": "please fix"})
    assert r.status_code == 200
    data = r.json()
    assert data["comment"] is not None
    assert data["comment"]["body"] == "please fix"
    assert data["ticket"]["state"] == "draft"

    comments = service.list_comments(t.id)
    assert len(comments) == 1
    assert comments[0].body == "please fix"


def test_request_changes_whitespace_body_treated_as_empty(client, service):
    """Whitespace-only body creates no comment."""
    t = service.create("RC test")
    service.transition(t.id, State.HUMAN_ISSUE_APPROVAL, note="refined")

    r = client.post(f"/tickets/{t.id}/request-changes", json={"body": "   "})
    assert r.status_code == 200
    data = r.json()
    assert data["comment"] is None

    comments = service.list_comments(t.id)
    assert len(comments) == 0


def test_request_changes_wrong_state_409(client, service):
    """POST /tickets/{id}/request-changes on non-human_issue_approval returns 409."""
    t = service.create("wrong state")
    service.transition(t.id, State.READY, note="refined (autonomous)")

    r = client.post(f"/tickets/{t.id}/request-changes", json={"body": "x"})
    assert r.status_code == 409


def test_request_changes_missing_ticket_404(client):
    """POST /tickets/{id}/request-changes with bogus id returns 404."""
    r = client.post("/tickets/nonexistent/request-changes", json={"body": "x"})
    assert r.status_code == 404


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
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="PR opened")
    service.transition(t.id, State.DONE, note="merged")
    service.transition(t.id, State.BLOCKED, note="retrospect failed")

    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 200
    data = r.json()
    assert data["state"] == State.DONE


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
        Path(robotsix_mill.runtime.board_html.__file__).parent / "static" / "board.js"
    )
    js = js_path.read_text()
    assert js.count("`") % 2 == 0, "unbalanced template-literal backticks"
    assert '</button>":' not in js  # the exact past defect
    assert "</button>`:" in js  # correctly-closed literal


def test_board_js_escapes_js_string_handlers(client):
    """Inline onclick handlers that embed a dynamic id must route it
    through the JS-string-context escaper jsq() — not esc() inside a
    single-quoted JS literal, and not a bare template-literal '${...}'.
    esc() escapes only [&<>], so a value containing a "'" would break (or
    inject into) the generated handler; jsq() emits a properly-quoted,
    HTML-attribute-safe JS string literal."""
    js = client.get("/static/mill/board-mill.js").text
    # The escaper is defined (function declaration in board-mill.js).
    assert "function jsq(" in js, "board-mill.js must define a jsq() JS-string escaper"
    # Proposals / candidate / survey / child-ticket handlers use jsq().
    assert "approveProposal(' + jsq(" in js
    assert "rejectProposal(' + jsq(" in js
    assert "rejectCandidate(' + jsq(" in js
    assert "open_(' + jsq(" in js  # survey / proposals link handlers
    # The unsafe pre-fix patterns must be gone: no id interpolated into a
    # single-quoted JS literal via esc(), and no bare-template open_('${id}').
    assert "esc(pa.id)" not in js
    assert "open_('${" not in js


def test_board_js_includes_origin_session_rendering(client):
    """The mill board overlay JS includes origin_session and
    origin_session_url rendering logic for the ticket detail drawer."""
    js = client.get("/static/mill/board-mill.js").text
    assert "origin_session_url" in js
    assert "origin_session" in js
    assert "origin-link" in js


def test_board_css_includes_origin_link_style(client):
    """The mill board overlay CSS includes the .origin-link style rule."""
    css = client.get("/static/mill/board-mill.css").text
    assert ".origin-link" in css


def test_origin_session_url_computed_when_config_set(service, settings, secrets_set):
    """enrich_ticket_read computes origin_session_url when all config
    ingredients are present."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("URL test", origin_session="sess-abc")
    secrets_set(
        langfuse_base_url="https://cloud.langfuse.com",
        langfuse_project_name="proj-xyz",
    )

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


def test_origin_session_url_with_repo_config(service, settings, repo_config):
    """enrich_ticket_read computes origin_session_url when a repo_config
    is provided (primary path)."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("Repo config URL test", origin_session="sess-xyz")
    tr = enrich_ticket_read(t, settings, service, repo_config=repo_config)
    assert tr.origin_session == "sess-xyz"
    assert tr.origin_session_url == (
        "https://cloud.langfuse.com/project/test-project/sessions/sess-xyz"
    )


def test_origin_session_url_empty_base_url_fallback(service, settings, repo_config):
    """enrich_ticket_read falls back to cloud.langfuse.com when
    repo_config.langfuse_base_url is empty."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("Empty base URL test", origin_session="sess-1")
    repo_config.langfuse_base_url = ""
    tr = enrich_ticket_read(t, settings, service, repo_config=repo_config)
    assert tr.origin_session_url == (
        "https://cloud.langfuse.com/project/test-project/sessions/sess-1"
    )


def test_origin_session_url_secrets_project_name_preferred(
    service, settings, secrets_set
):
    """secrets fallback prefers langfuse_project_name over langfuse_project_id."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("Name preferred test", origin_session="sess-2")
    secrets_set(
        langfuse_base_url="https://custom.lf.example.com",
        langfuse_project_name="my-project-name",
        langfuse_project_id="proj-old-id",
    )
    tr = enrich_ticket_read(t, settings, service)
    assert tr.origin_session_url == (
        "https://custom.lf.example.com/project/my-project-name/sessions/sess-2"
    )


def test_origin_session_url_secrets_project_id_fallback(service, settings, secrets_set):
    """secrets fallback uses langfuse_project_id when langfuse_project_name is absent."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("ID fallback test", origin_session="sess-3")
    secrets_set(
        langfuse_base_url="https://cloud.langfuse.com",
        langfuse_project_id="proj-legacy",
    )
    tr = enrich_ticket_read(t, settings, service)
    assert tr.origin_session_url == (
        "https://cloud.langfuse.com/project/proj-legacy/sessions/sess-3"
    )


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
    # robotsix-board's board.js contains "robotsixBoardRefresh"; the
    # legacy bundled board.js contains "refresh".  Either is valid.
    assert "refresh" in js.text or "robotsixBoardRefresh" in js.text
    # The drawer close handler is always present (either "open_" from
    # the legacy bundle or "openDrawer" from robotsix-board).
    assert any(
        name in js.text for name in ("open_", "openDrawer", "closeDrawer")
    )


def test_audit_endpoint_is_fire_and_forget(client, monkeypatch):
    """Regression: POST /audit ran the LLM agent synchronously, so the
    browser fetch hung for minutes and dropped ('NetworkError'). It
    must return 202 immediately and run the audit in the background."""
    import threading

    from robotsix_mill.runners import audit_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_audit(session_id=None, repo_config=None):
        ran.set()
        release.wait(5)  # simulate a minutes-long run
        return _R()

    monkeypatch.setattr(audit_runner, "run_audit_pass", slow_audit)

    r = client.post("/audit")  # must NOT block on slow_audit
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)  # audit really started in the background
    release.set()  # let the daemon thread finish


def test_agent_check_endpoint_is_fire_and_forget(client, monkeypatch):
    """POST /agent-check returns 202 immediately and runs the
    agent-check agent in the background — same fire-and-forget
    contract as /audit, /health-check, /trace-health."""
    import threading

    from robotsix_mill.runners import agent_check_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_agent_check(session_id=None, repo_config=None):
        ran.set()
        release.wait(5)
        return _R()

    monkeypatch.setattr(agent_check_runner, "run_agent_check_pass", slow_agent_check)

    r = client.post("/agent-check")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)
    release.set()


def test_run_health_endpoint_is_fire_and_forget(client, monkeypatch):
    """POST /run-health returns 202 immediately and runs the run-health
    pass in the background — same fire-and-forget contract as /audit,
    /health-check, /cost-analyst. It is a global pass (no repo_id)."""
    import threading

    from robotsix_mill.runners import run_health_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_run_health(session_id=None):
        ran.set()
        release.wait(5)  # simulate a minutes-long run
        return _R()

    monkeypatch.setattr(run_health_runner, "run_run_health_pass", slow_run_health)

    r = client.post("/run-health")  # must NOT block on slow_run_health
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)  # run-health really started in the background
    release.set()  # let the daemon thread finish


def test_board_html_includes_agent_check_button(client):
    """The board exposes an 'Agent Check' button wired to
    runAgentCheck() in the JS. Without it the user can't see the
    agent-check feature exists, and only the CLI is discoverable."""
    body = client.get("/").text
    assert "Agent Check" in body
    assert "runAgentCheck()" in body
    # Mill-specific agent functions are in board-mill.js (layered on top
    # of robotsix-board's shared board.js).
    js = client.get("/static/mill/board-mill.js").text
    assert "runAgentCheck" in js
    assert '"/agent-check"' in js


def test_board_cleanup_endpoint_is_fire_and_forget(client, monkeypatch):
    """POST /board-cleanup returns 202 immediately and runs the
    board-cleanup pass in the background — same fire-and-forget
    contract as /audit, /agent-check. The bespoke runner needs a real
    repo_config + settings, so the route substitutes the configured
    repos for the single-repo [None] sentinel."""
    import threading

    from robotsix_mill.runners import periodic_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        updated_memory: str = ""
        drafts_created: list = []

    def slow_board_cleanup(session_id=None, repo_config=None, settings=None, **kw):
        ran.set()
        release.wait(5)  # simulate a minutes-long run
        return _R()

    monkeypatch.setattr(periodic_runner, "run_board_cleanup_pass", slow_board_cleanup)

    r = client.post("/board-cleanup")  # must NOT block on slow_board_cleanup
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)  # board-cleanup really started in the background
    release.set()  # let the daemon thread finish


def test_board_html_includes_board_cleanup_button(client):
    """The board exposes a 'Board Cleanup' button wired to
    runBoardCleanup() in the JS. Without it the user can't see the
    board-cleanup feature exists, and only the periodic scheduler can
    trigger it."""
    body = client.get("/").text
    assert "Board Cleanup" in body
    assert "runBoardCleanup()" in body
    js = client.get("/static/mill/board-mill.js").text
    assert "runBoardCleanup" in js
    assert '"/board-cleanup"' in js


def test_board_html_includes_cost_analyst_button(client):
    """The board exposes a 'Cost Analyst' button wired to
    runCostAnalyst() in the JS. Without it the user can't trigger the
    cost-analyst pass from the board, and only the CLI is discoverable."""
    body = client.get("/").text
    assert "Cost Analyst" in body
    assert "runCostAnalyst()" in body
    js = client.get("/static/mill/board-mill.js").text
    assert "runCostAnalyst" in js
    assert '"/cost-analyst"' in js


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
    # POST /tickets is in the mill overlay JS (now via the XHR helper,
    # not fetch — fetch is wrapped by SES/extensions and unreliable).
    js = client.get("/static/mill/board-mill.js").text
    assert 'jpost("/tickets"' in js


def test_board_has_new_inquiry_affordance(client):
    """The board exposes a '+ Ask' button wired to POST /tickets with kind='inquiry'.

    Regression guard: the inquiry backend landed but the button was
    forgotten (same as the comment-UI gap). This assertion prevents recurrence.
    """
    body = client.get("/").text
    assert "newInquiry()" in body
    assert "+ Ask" in body
    js = client.get("/static/mill/board-mill.js").text
    assert "newInquiry" in js
    # The only thing that distinguishes inquiry creation from task creation:
    assert 'kind: "inquiry"' in js, (
        "newInquiry() must POST kind='inquiry', not the default 'task' — "
        "without this the button silently creates tasks instead of inquiries"
    )


def test_board_has_manual_child_ticket_affordance(client):
    """The board exposes an 'Add Ticket' button inside epic drawers so users
    can manually create child tickets without relying on the LLM breakdown."""
    js = client.get("/static/mill/board-mill.js").text
    assert "newChildTicket" in js, "board-mill.js must define newChildTicket()"
    assert "Add Ticket" in js, "board-mill.js must render an Add Ticket button"
    assert "parent_id: epicId" in js, (
        "newChildTicket() must pass parent_id to POST /tickets"
    )
    assert 'kind: "task"' in js, (
        "newChildTicket() must create child tickets as kind='task'"
    )
    assert "open_(epicId)" in js, (
        "newChildTicket() must re-render the epic drawer on success, not refresh()"
    )


def test_post_tickets_creates_user_draft(client):
    """The control's backend: POST /tickets -> a DRAFT, source=user."""
    r = client.post("/tickets", json={"title": "From the board", "description": "idea"})
    assert r.status_code == 201
    d = r.json()
    assert d["state"] == "draft"
    assert d["source"] == SourceKind.USER


def test_post_tickets_with_kind_inquiry_creates_asked_inquiry(client):
    """POST /tickets with kind='inquiry' creates an inquiry in ASKED state.

    This is the backend path the '+ Ask' button drives.
    """
    r = client.post(
        "/tickets",
        json={
            "title": "Why does X happen?",
            "description": "context",
            "kind": "inquiry",
        },
    )
    assert r.status_code == 201
    d = r.json()
    assert d["state"] == "asked"  # inquiries start in ASKED, not DRAFT
    assert d["kind"] == "inquiry"
    assert d["source"] == SourceKind.USER


# --- depends_on API ----------------------------------------------------


def test_create_ticket_with_depends_on(client):
    """POST /tickets accepts depends_on and the field is present in the response."""
    r = client.post(
        "/tickets",
        json={
            "title": "Dep ticket API",
            "depends_on": '["ticket-aaa", "ticket-bbb"]',
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["depends_on"] == '["ticket-aaa", "ticket-bbb"]'
    assert "unmet_deps" in data


def test_get_ticket_includes_depends_on_and_unmet_deps(client):
    """GET /tickets/{id} includes depends_on and unmet_deps fields."""
    r = client.post(
        "/tickets",
        json={
            "title": "With dep",
            "depends_on": '["some-other-ticket"]',
        },
    )
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
    r = client.post(
        "/tickets",
        json={
            "title": "List dep test",
            "depends_on": '["x", "y"]',
        },
    )
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


def test_list_tickets_include_closed_hides_closed_and_epic_closed_keeps_done(
    client, service
):
    """include_closed=false must hide CLOSED and EPIC_CLOSED but ALWAYS
    return DONE — DONE is the transient retrospect-in-flight window and
    needs to stay visible so the board can show retrospect work without
    the user toggling 'Show closed.'"""
    # Create via the service (not the API) to bypass maybe_enqueue —
    # the worker would otherwise refine these tickets and BLOCK them
    # on the missing API key, racing the transitions below.
    closed = service.create("C-closed")
    done = service.create("C-done")
    draft = service.create("C-draft")
    epic = service.create("C-epic", kind="epic")
    # Walk via legal edges: DRAFT -> DONE (refine's dedup-discard route),
    # DONE -> CLOSED (retrospect's edge), EPIC_OPEN -> EPIC_CLOSED.
    service.transition(closed.id, State.DONE)
    service.transition(closed.id, State.CLOSED)
    service.transition(done.id, State.DONE)
    service.transition(epic.id, State.EPIC_CLOSED)

    # include_closed=true → everything visible.
    ids_all = {t["id"] for t in client.get("/tickets").json()}
    assert {closed.id, done.id, draft.id, epic.id} <= ids_all

    # include_closed=false → CLOSED + EPIC_CLOSED hidden, DONE + DRAFT still visible.
    ids = {t["id"] for t in client.get("/tickets?include_closed=false").json()}
    assert done.id in ids, "DONE must stay visible (retrospect-in-flight)"
    assert draft.id in ids
    assert closed.id not in ids, "CLOSED must be hidden by the toggle"
    assert epic.id not in ids, "EPIC_CLOSED must be hidden by the toggle"


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
    """The mill board overlay JS renders ``dependencies`` (structured
    per-dep status) and surfaces the ``unmet_deps`` waiting count."""
    js = client.get("/static/mill/board-mill.js").text
    assert "t.dependencies" in js
    assert "depends on:" in js
    assert "unmet_deps" in js
    assert "waiting on" in js


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


def test_create_epic_via_api_repo_resolution(client):
    """POST /epics in single-repo mode auto-selects the repo without repo_id."""
    r = client.post("/epics", json={"title": "No repo_id given"})
    assert r.status_code == 201
    data = r.json()
    assert data["state"] == "epic_open"
    assert data["kind"] == "epic"
    # The ticket should have been placed on the lone board.
    assert data.get("board_id") is not None


def test_create_epic_missing_title(client):
    """POST /epics with empty title returns 400."""
    r = client.post("/epics", json={"title": "", "description": "desc"})
    assert r.status_code == 400
    assert "title is required" in r.json()["detail"]


def test_create_epic_via_cli_pattern(client):
    """The CLI 'epic new' flow hits POST /epics — validate end-to-end shape."""
    r = client.post(
        "/epics",
        json={
            "title": "CLI-created epic",
            "description": "From the terminal",
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert data["state"] == "epic_open"
    assert data["kind"] == "epic"
    assert data["title"] == "CLI-created epic"
    # CLI prints the id — verify it's present and non-empty.
    assert data["id"]


def test_create_ticket_with_parent(client, service):
    """POST /tickets with parent_id set links child to epic."""
    epic = service.create("Epic", kind="epic")
    r = client.post(
        "/tickets",
        json={
            "title": "Child Task",
            "description": "detail",
            "parent_id": epic.id,
        },
    )
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
    from robotsix_mill.core.service import TicketService

    epic = service.create("Break me down", kind="epic")

    # Signal when the background thread has created both children.
    # Patch at the CLASS level — the route builds a fresh per-board
    # TicketService for multi-repo correctness, so any per-instance
    # patching of the app's service is bypassed.
    children_created = threading.Event()
    child_count = [0]
    orig_create = TicketService.create

    def tracking_create(self, title, *args, **kwargs):
        result = orig_create(self, title, *args, **kwargs)
        child_count[0] += 1
        if child_count[0] >= 2:
            children_created.set()
        return result

    monkeypatch.setattr(TicketService, "create", tracking_create)
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


def test_generate_children_flags_overlapping_child(client, service, monkeypatch):
    """The /generate-children route runs the advisory pre-filing dedup
    check: two overlapping children (shared CONTRIBUTING.md path) are
    BOTH created, and exactly the later one carries the ``[!warning]``
    advisory block — never silently dropped."""
    import threading

    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult
    from robotsix_mill.core.service import TicketService

    epic = service.create("Audit Trivy SARIF", kind="epic")

    children_created = threading.Event()
    child_count = [0]
    orig_create = TicketService.create

    def tracking_create(self, title, *args, **kwargs):
        result = orig_create(self, title, *args, **kwargs)
        child_count[0] += 1
        if child_count[0] >= 2:
            children_created.set()
        return result

    monkeypatch.setattr(TicketService, "create", tracking_create)
    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        lambda **kw: EpicBreakdownResult(
            child_titles=["First Trivy child", "Second Trivy child"],
            child_bodies=[
                "Work documented in CONTRIBUTING.md for the first child",
                "Work documented in CONTRIBUTING.md for the second child",
            ],
        ),
    )

    r = client.post(f"/tickets/{epic.id}/generate-children")
    assert r.status_code == 202
    assert children_created.wait(5), "children were not created in time"

    children = service.list_children(epic.id)
    assert len(children) == 2, "both children must be created, none dropped"
    bodies = [service.workspace(c).read_description() for c in children]
    flagged = [b for b in bodies if "[!warning]" in b]
    assert len(flagged) == 1
    assert "CONTRIBUTING.md" in flagged[0]


def test_generate_children_applies_epic_body(client, service, monkeypatch):
    """POST /tickets/{id}/generate-children writes the agent's epic_body
    back to the epic's description.md."""
    import threading

    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult
    from robotsix_mill.core.service import TicketService

    epic = service.create("Break me down", "Original epic description", kind="epic")

    # Signal when the background thread has written the epic body.
    # Patch at the CLASS level for the same multi-repo reason as
    # the sibling test_generate_children_creates_children above.
    epic_updated = threading.Event()
    orig_set_hash = TicketService.set_content_hash

    def tracking_set_hash(self, ticket_id, new_hash):
        orig_set_hash(self, ticket_id, new_hash)
        if ticket_id == epic.id:
            epic_updated.set()

    monkeypatch.setattr(TicketService, "set_content_hash", tracking_set_hash)
    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        lambda **kw: EpicBreakdownResult(
            child_titles=["Child A"],
            child_bodies=["Body A"],
            epic_body="Revised epic strategy: break into auth, roles, audit.",
        ),
    )

    r = client.post(f"/tickets/{epic.id}/generate-children")
    assert r.status_code == 202

    # Wait for the background thread to apply the epic body.
    assert epic_updated.wait(5), "epic body was not applied in time"

    # Epic description should now contain the revised body.
    epic_desc = service.workspace(epic).read_description()
    assert "Revised epic strategy" in epic_desc
    assert "auth, roles, audit" in epic_desc


# ---------------------------------------------------------------------------
# add_comment → epic re-processing tests
# ---------------------------------------------------------------------------


def test_add_comment_on_epic_triggers_reprocess(client, service, monkeypatch):
    """Comment on an epic spawns a background thread that calls the
    breakdown agent and creates new children."""
    import time

    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult

    epic = service.create("Epic to comment on", "Epic desc", kind="epic")

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        lambda **kw: EpicBreakdownResult(
            child_titles=["New child from comment"],
            child_bodies=["Body from comment"],
        ),
    )

    r = client.post(
        f"/tickets/{epic.id}/comments",
        json={"body": "Break this into a new child please"},
    )
    assert r.status_code == 201
    comment = r.json()
    assert comment["body"] == "Break this into a new child please"

    # Agent runs in background; poll for children.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        children = client.get(f"/tickets/{epic.id}/children").json()
        if len(children) >= 1:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("children were not created within timeout")

    assert len(children) == 1
    assert children[0]["title"] == "New child from comment"


def test_add_comment_on_non_epic_does_not_trigger_reprocess(
    client,
    service,
    monkeypatch,
):
    """Comment on a task ticket does NOT invoke the breakdown agent."""
    import threading

    agent_called = threading.Event()

    def fake_agent(**kw):
        agent_called.set()
        from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult

        return EpicBreakdownResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        fake_agent,
    )

    task = service.create("A task ticket", kind="task")
    r = client.post(
        f"/tickets/{task.id}/comments",
        json={"body": "This should not trigger anything"},
    )
    assert r.status_code == 201
    assert r.json()["body"] == "This should not trigger anything"

    # The agent must NOT have been called.
    assert not agent_called.wait(1), "breakdown agent was called for a non-epic ticket"


def test_add_comment_on_epic_skips_duplicate_children(
    client,
    service,
    monkeypatch,
):
    """When the agent proposes a child whose title already exists
    (case-insensitive), it is skipped — no duplicate created."""
    import time

    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult

    epic = service.create("Epic with existing child", kind="epic")
    service.create("Add auth module", kind="task", parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        lambda **kw: EpicBreakdownResult(
            child_titles=["Add auth module", "Add payment module"],
            child_bodies=["Body auth", "Body payment"],
        ),
    )

    r = client.post(
        f"/tickets/{epic.id}/comments",
        json={"body": "Add payment module"},
    )
    assert r.status_code == 201

    # Poll for children to be created.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        children = client.get(f"/tickets/{epic.id}/children").json()
        if len(children) >= 2:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("children were not created within timeout")

    titles = {c["title"] for c in children}
    assert titles == {"Add auth module", "Add payment module"}
    # Exactly one new child created, not two.
    assert len(children) == 2


def test_add_comment_on_epic_response_is_immediate(client, service, monkeypatch):
    """The HTTP response for add_comment returns 201 immediately —
    the agent runs in a daemon thread and does not block the response."""
    import threading

    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult

    epic = service.create("Slow epic", kind="epic")

    ran = threading.Event()
    release = threading.Event()

    def slow_agent(**kw):
        ran.set()
        release.wait(5)
        return EpicBreakdownResult()

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        slow_agent,
    )

    r = client.post(
        f"/tickets/{epic.id}/comments",
        json={"body": "Trigger slow agent"},
    )
    assert r.status_code == 201
    assert r.json()["body"] == "Trigger slow agent"
    assert ran.wait(5), "agent did not start in background"

    # Clean up: let the daemon finish.
    release.set()


def test_add_comment_comment_history_reaches_agent(client, service, monkeypatch):
    """When comments exist on an epic, the full history is passed to
    the breakdown agent via the *comments* parameter."""
    import time

    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult

    epic = service.create("Epic with history", "Epic desc", kind="epic")
    service.add_comment(epic.id, "First comment")
    service.add_comment(epic.id, "Second comment")

    agent_comments: list[str] = []

    def capture_agent(**kw):
        agent_comments.append(kw.get("comments", ""))
        return EpicBreakdownResult(
            child_titles=["Child 1"],
            child_bodies=["Body 1"],
        )

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        capture_agent,
    )

    r = client.post(
        f"/tickets/{epic.id}/comments",
        json={"body": "Third comment — the trigger"},
    )
    assert r.status_code == 201

    # Poll for the child to appear (agent ran in background).
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        children = client.get(f"/tickets/{epic.id}/children").json()
        if len(children) >= 1:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("child was not created within timeout")

    assert len(agent_comments) == 1
    comments_str = agent_comments[0]
    assert "First comment" in comments_str
    assert "Second comment" in comments_str
    assert "Third comment" in comments_str


# ---------------------------------------------------------------------------
# Cumulative cost tests
# ---------------------------------------------------------------------------


def test_epic_detail_cost_is_cumulative(client, service, monkeypatch):
    """GET /tickets/{epic_id} returns cost_usd = epic's own session cost,
    and cumulative_cost = epic own cost + all children."""
    epic = service.create("Epic", kind="epic")
    c1 = service.create("Child 1", kind="task", parent_id=epic.id)
    c2 = service.create("Child 2", kind="task", parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: {
            epic.id: 0.01,
            c1.id: 0.10,
            c2.id: 0.20,
        }.get(sid, 0.0),
    )

    r = client.get(f"/tickets/{epic.id}").json()
    assert r["cost_usd"] == pytest.approx(0.01)  # epic's own session cost
    assert r["cumulative_cost"] == pytest.approx(0.31)  # 0.01 + 0.10 + 0.20


def test_epic_list_cost_is_cache_only(client, service, monkeypatch):
    """GET /tickets builds its RESPONSE cache-only — epic cumulative cost must
    not trigger blocking session_cost during the request. (Children are still
    warmed afterwards by the background task, which is fine.)"""
    epic = service.create("Epic", kind="epic")
    service.create("Child 1", kind="task", parent_id=epic.id)
    service.create("Child 2", kind="task", parent_id=epic.id)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: 0.999,
    )

    ts = client.get("/tickets").json()
    epic_entry = [x for x in ts if x["id"] == epic.id]
    assert len(epic_entry) == 1
    # RESPONSE is cache-only: epic's own cost 0.0 and cumulative not computed,
    # even though session_cost returns 0.999.
    assert epic_entry[0]["cost_usd"] == 0.0
    assert epic_entry[0]["cumulative_cost"] is None


def test_nested_epic_cost_is_recursive(client, service, monkeypatch):
    """Epic → sub-epic → task: top epic cumulative includes all three,
    but cost_usd stays as its own direct session cost."""
    e1 = service.create("E1", kind="epic")
    e2 = service.create("E2", kind="epic", parent_id=e1.id)
    t = service.create("T", kind="task", parent_id=e2.id)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: {e1.id: 0.01, e2.id: 0.02, t.id: 0.30}.get(
            sid, 0.0
        ),
    )

    r1 = client.get(f"/tickets/{e1.id}").json()
    assert r1["cost_usd"] == pytest.approx(0.01)  # e1's own session cost
    assert r1["cumulative_cost"] == pytest.approx(0.33)  # 0.01 + 0.02 + 0.30

    r2 = client.get(f"/tickets/{e2.id}").json()
    assert r2["cost_usd"] == pytest.approx(0.02)  # e2's own session cost
    assert r2["cumulative_cost"] == pytest.approx(0.32)  # 0.02 + 0.30


def test_ticket_with_children_has_cumulative_cost(client, service, monkeypatch):
    """A non-epic ticket with child tickets gets cumulative_cost > cost_usd."""
    parent = service.create("Parent task", kind="task")
    c1 = service.create("Child 1", kind="task", parent_id=parent.id)
    c2 = service.create("Child 2", kind="task", parent_id=parent.id)

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: {
            parent.id: 0.05,
            c1.id: 0.10,
            c2.id: 0.07,
        }.get(sid, 0.0),
    )

    r = client.get(f"/tickets/{parent.id}").json()
    assert r["cost_usd"] == pytest.approx(0.05)
    assert r["cumulative_cost"] == pytest.approx(0.22)  # 0.05 + 0.10 + 0.07


def test_leaf_ticket_cumulative_cost_is_none(client, service, monkeypatch):
    """A ticket with no children has cumulative_cost: null in JSON."""
    leaf = service.create("Leaf task", kind="task")

    monkeypatch.setattr(
        "robotsix_mill.langfuse.client.session_cost",
        lambda settings, sid, **kw: 0.042 if sid == leaf.id else 0.0,
    )

    r = client.get(f"/tickets/{leaf.id}").json()
    assert r["cost_usd"] == pytest.approx(0.042)
    assert r["cumulative_cost"] is None


def test_board_js_references_cumulative_cost(client):
    """board-mill.js contains references to cumulative_cost for the split
    badge and drawer rendering."""
    js = client.get("/static/mill/board-mill.js").text
    assert "cumulative_cost" in js


# ---------------------------------------------------------------------------
# merge-now / merge-reason tests
# ---------------------------------------------------------------------------


class _FakeForge:
    """Minimal forge stub for merge-now endpoint tests."""

    _UNSET = object()

    def __init__(self, merge_result=_UNSET, pr_status_result=_UNSET):
        if merge_result is self._UNSET:
            merge_result = {"merged": True, "reason": "merged"}
        if pr_status_result is self._UNSET:
            pr_status_result = {
                "url": "https://github.com/test/pr/1",
                "merged": False,
                "state": "open",
                "mergeable": True,
            }
        self._merge_result = merge_result
        self._pr_status_result = pr_status_result
        self.merge_calls: list[dict] = []
        self.pr_status_calls: list[dict] = []

    def merge_pr(self, *, source_branch: str) -> dict:
        self.merge_calls.append({"source_branch": source_branch})
        return self._merge_result

    def pr_status(self, *, source_branch: str) -> dict | None:
        self.pr_status_calls.append({"source_branch": source_branch})
        return self._pr_status_result


def _patch_forge(monkeypatch, fake_forge):
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets.get_forge",
        lambda s, repo_config=None: fake_forge,
    )


def test_merge_now_happy_path(client, service, monkeypatch):
    """POST /tickets/{id}/merge-now on human_mr_approval merges and
    transitions to done."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = service.create("Merge me please")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="awaiting merge")
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    data = r.json()
    assert data["id"] == t.id
    assert data["state"] == "done"

    # Verify the forge was called with the right branch.
    assert len(fake.merge_calls) == 1
    assert fake.merge_calls[0]["source_branch"] == t.branch

    # Verify the history contains the merge note.
    history = service.history(t.id)
    notes = " ".join(e.note or "" for e in history)
    assert "merged via board" in notes


def test_merge_now_wrong_state_409(client, service, monkeypatch):
    """POST /tickets/{id}/merge-now on a non-human_mr_approval ticket
    returns 409."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)

    t = service.create("Ready ticket")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    assert service.get(t.id).state is State.READY

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409
    assert "not in human_mr_approval" in r.text.lower()

    # Forge should never have been called.
    assert len(fake.merge_calls) == 0


def test_merge_now_missing_ticket_404(client, monkeypatch):
    """POST /tickets/{id}/merge-now with a bogus id returns 404."""
    fake = _FakeForge()
    _patch_forge(monkeypatch, fake)
    r = client.post("/tickets/nonexistent/merge-now")
    assert r.status_code == 404


def test_merge_now_forge_rejection_409(client, service, monkeypatch):
    """POST /tickets/{id}/merge-now when the forge rejects returns 409
    and leaves the ticket state unchanged."""
    fake = _FakeForge(
        merge_result={"merged": False, "reason": "branch protection rules"},
    )
    _patch_forge(monkeypatch, fake)

    t = service.create("Blocked merge")
    service.transition(t.id, State.READY, note="approved (autonomous)")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="awaiting merge")
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409
    assert "branch protection rules" in r.text

    # Ticket state must be unchanged.
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL


def test_merge_reason_returns_file(client, service):
    """GET /tickets/{id}/merge-reason returns the contents of
    merge_reason.txt from the workspace."""
    t = service.create("Reason ticket")
    reason_path = service.workspace(t).artifacts_dir / "merge_reason.txt"
    reason_path.parent.mkdir(parents=True, exist_ok=True)
    reason_path.write_text("auto-merge disabled in config", encoding="utf-8")

    r = client.get(f"/tickets/{t.id}/merge-reason")
    assert r.status_code == 200
    assert r.json() == {"reason": "auto-merge disabled in config"}


def test_merge_reason_empty_when_no_file(client, service):
    """GET /tickets/{id}/merge-reason returns an empty reason when the
    file doesn't exist."""
    t = service.create("No reason file ticket")

    r = client.get(f"/tickets/{t.id}/merge-reason")
    assert r.status_code == 200
    assert r.json() == {"reason": ""}


def test_merge_reason_missing_ticket_404(client):
    """GET /tickets/{id}/merge-reason with a bogus id returns 404."""
    r = client.get("/tickets/nonexistent/merge-reason")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /agents — per-repo enabled on-demand agent names
# ---------------------------------------------------------------------------


def _seed_periodic_clone(settings, repo_id, names, *, disabled=()):
    """Create a fake per-repo clone with ``.robotsix-mill/periodic/<name>.yaml``
    files so ``_find_config_clone_dir`` + ``discover_periodic_workflows``
    resolve them (presence = enabled, unless the file sets enabled: false)."""
    from pathlib import Path

    clone = Path(settings.data_dir) / repo_id / "periodic_workspace" / "repo"
    (clone / ".git").mkdir(parents=True, exist_ok=True)
    pdir = clone / ".robotsix-mill" / "periodic"
    pdir.mkdir(parents=True, exist_ok=True)
    for n in names:
        body = f"name: {n}\n"
        if n in disabled:
            body += "enabled: false\n"
        (pdir / f"{n}.yaml").write_text(body, encoding="utf-8")
    return clone


def test_agents_lists_enabled_for_repo(client, settings):
    """GET /agents?repo_id=<id> returns the periodic-agent names enabled
    for that repo (file presence under .robotsix-mill/periodic/)."""
    _seed_periodic_clone(settings, "test-repo", ["audit", "health"])
    r = client.get("/agents?repo_id=test-repo")
    assert r.status_code == 200
    assert sorted(r.json()) == ["audit", "health"]


def test_agents_respects_disabled_yaml_and_kill_switch(client, settings):
    """An agent whose file sets enabled: false is skipped, and so is one
    whose fleet-wide Settings.<name>_periodic kill-switch is False."""
    _seed_periodic_clone(
        settings, "test-repo", ["audit", "health", "survey"], disabled=["survey"]
    )
    settings.health_periodic = False  # fleet-wide kill-switch off
    r = client.get("/agents?repo_id=test-repo")
    assert r.status_code == 200
    # survey skipped (enabled: false), health skipped (kill-switch False).
    assert r.json() == ["audit"]


def test_agents_all_or_omitted_returns_empty_list(client):
    """GET /agents with repo_id omitted, 'all', or unknown returns an
    empty list (HTTP 200, no error)."""
    r_omitted = client.get("/agents")
    assert r_omitted.status_code == 200
    assert r_omitted.json() == []

    r_all = client.get("/agents?repo_id=all")
    assert r_all.status_code == 200
    assert r_all.json() == []

    r_unknown = client.get("/agents?repo_id=does-not-exist")
    assert r_unknown.status_code == 200
    assert r_unknown.json() == []
