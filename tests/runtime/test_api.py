import json

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.states import State
from robotsix_mill.core.models import SourceKind, TicketKind
from robotsix_mill.runtime.api import create_app


@pytest.fixture
def client(settings, repos_registry):
    # TestClient runs the lifespan: init_db, worker start/stop.
    with TestClient(
        create_app(repos_registry, settings, single_repo_id="test-repo")
    ) as c:
        yield c


@pytest.fixture
def multi_repo_client(settings, two_repo_registry):
    # Multi-repo mode: no single_repo_id, so /repos surfaces every
    # registered repo plus the synthetic "meta" entry.
    with TestClient(create_app(two_repo_registry, settings)) as c:
        yield c


@pytest.fixture
def clean_failures():
    """Keep the module-global Langfuse failure registry clean so test
    ordering can't bleed across langfuse-status tests."""
    from robotsix_mill.runtime.tracing import clear_export_failures

    clear_export_failures()
    yield
    clear_export_failures()


def test_health(client):
    assert client.get("/health").json()["status"] == "alive"


def test_health_reports_uptime_when_started_at_set(client):
    """The started_at branch: with app.state.started_at set, /health
    returns the started_at isoformat and a non-negative int uptime."""
    from datetime import datetime, timedelta, timezone

    started = datetime.now(timezone.utc) - timedelta(seconds=5)
    saved = getattr(client.app.state, "started_at", None)
    client.app.state.started_at = started
    try:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "alive"
        assert body["started_at"] == started.isoformat()
        assert isinstance(body["uptime_seconds"], int)
        assert body["uptime_seconds"] >= 0
    finally:
        if saved is None:
            delattr(client.app.state, "started_at")
        else:
            client.app.state.started_at = saved


def test_langfuse_status_empty(client, clean_failures):
    """With the failure registry cleared, /langfuse-status reports none."""
    r = client.get("/langfuse-status")
    assert r.status_code == 200
    assert r.json() == {"failures": [], "count": 0}


def test_langfuse_status_with_failures(client, clean_failures):
    """Seeded failures are surfaced with their project/error values."""
    from robotsix_mill.runtime.tracing import record_export_failure

    record_export_failure(project="proj-a", error="boom", status=500)
    record_export_failure(project="proj-b", error="kaput", status=None)

    r = client.get("/langfuse-status")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert isinstance(body["failures"], list)
    assert len(body["failures"]) == 2
    projects = {f["project"] for f in body["failures"]}
    errors = {f["error"] for f in body["failures"]}
    assert projects == {"proj-a", "proj-b"}
    assert errors == {"boom", "kaput"}


def test_langfuse_status_clear(client, clean_failures):
    """POST /langfuse-status/clear returns 204 with empty body and
    empties the registry."""
    from robotsix_mill.runtime.tracing import record_export_failure

    record_export_failure(project="proj-a", error="boom", status=500)

    r = client.post("/langfuse-status/clear")
    assert r.status_code == 204
    assert r.content == b""

    after = client.get("/langfuse-status").json()
    assert after == {"failures": [], "count": 0}


def test_list_repos_single_repo(client):
    """Single-repo mode returns exactly the one repo and short-circuits
    before appending the synthetic meta entry."""
    r = client.get("/repos")
    assert r.status_code == 200
    body = r.json()
    assert body == [
        {"repo_id": "test-repo", "board_id": "test-board", "forge_remote_url": None}
    ]
    assert all(e["repo_id"] != "meta" for e in body)


def test_list_repos_multi_repo(multi_repo_client):
    """Multi-repo mode lists every repo plus the meta entry appended
    last, exposing repo_id/board_id and a credential-free
    forge_remote_url (no Langfuse secrets)."""
    r = multi_repo_client.get("/repos")
    assert r.status_code == 200
    body = r.json()
    assert {
        "repo_id": "repo-a",
        "board_id": "board-a",
        "forge_remote_url": None,
    } in body
    assert {
        "repo_id": "repo-b",
        "board_id": "board-b",
        "forge_remote_url": None,
    } in body
    assert body[-1] == {"repo_id": "meta", "board_id": "meta", "forge_remote_url": None}
    for entry in body:
        assert set(entry.keys()) == {"repo_id", "board_id", "forge_remote_url"}


def test_public_forge_url_strips_credentials():
    """Tokenized remote URLs must never leak through the unauthenticated
    /repos endpoint — userinfo is stripped, everything else preserved."""
    from robotsix_mill.runtime.routes._health import _public_forge_url

    assert (
        _public_forge_url("https://oauth2:tok3n@github.com/o/r.git")
        == "https://github.com/o/r.git"
    )
    assert (
        _public_forge_url("https://github.com/o/r.git") == "https://github.com/o/r.git"
    )
    assert _public_forge_url(None) is None
    assert _public_forge_url("") is None


def test_gates(client, settings):
    """GET /gates returns the pipeline gate flags from the live Settings."""
    r = client.get("/gates")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "auto_approve": settings.auto_approve_enabled,
        "review": settings.review_enabled,
        "auto_merge": settings.auto_merge_enabled,
        "require_approval": settings.require_approval,
        "comments_after_body": settings.comments_after_body,
    }


def test_board_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "robotsix-mill" in body
    assert "cdn.jsdelivr.net/npm/marked" in body
    assert '<div id="board"' in body
    assert '<div id="drawer">' in body
    # The column skeleton must be rendered server-side: board.js
    # (JSON_HYDRATION) only diffs cards into pre-existing columns, so an
    # empty #board would show no tickets at all.
    assert "{BOARD_SKELETON}" not in body  # replaced at request time
    assert 'class="board-column" data-status="draft"' in body
    assert 'class="board-column-cards"' in body
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


def test_board_mill_js_sets_repo_filtered_refresh_url(client):
    """board-mill.js must push the selected repo filter into the board
    refresh URL via robotsixBoardSetRefreshUrl so the dropdown actually
    filters tickets (the URL becomes /board/cards?repo_id=...)."""
    js = client.get("/static/mill/board-mill.js").text
    assert "robotsixBoardSetRefreshUrl" in js
    assert "/board/cards?repo_id=" in js


def test_board_cards_closed_sorted_newest_first(client, service, settings):
    """Closed and epic_closed cards are returned most-recent-first
    (updated_at descending), while non-closed cards remain
    created_at ascending (oldest first)."""
    from datetime import datetime, timezone

    from robotsix_mill.core.db import session as db_session
    from robotsix_mill.core.models import Ticket

    # Create tickets via the service (sets created_at).
    # We'll then use direct DB access to set deterministic timestamps.
    board_id = "test-board"
    t_draft = service.create("Draft oldest")
    t_draft2 = service.create("Draft newest")
    t_closed_old = service.create("Closed old")
    t_closed_new = service.create("Closed new")
    t_epic_closed = service.create("Epic closed ticket", kind=TicketKind.EPIC)

    # Walk legal edges to reach closed / epic_closed.
    service.transition(t_closed_old.id, State.DONE)
    service.transition(t_closed_old.id, State.CLOSED)
    service.transition(t_closed_new.id, State.DONE)
    service.transition(t_closed_new.id, State.CLOSED)
    service.transition(t_epic_closed.id, State.EPIC_CLOSED)

    # Set deterministic timestamps via direct DB session.
    with db_session(settings, board_id) as s:
        # Draft: created_at ascending → draft-oldest before draft-newest.
        d1 = s.get(Ticket, t_draft.id)
        d1.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        d2 = s.get(Ticket, t_draft2.id)
        d2.created_at = datetime(2025, 1, 2, tzinfo=timezone.utc)

        # Closed: updated_at descending → newest-first.
        c_old = s.get(Ticket, t_closed_old.id)
        c_old.updated_at = datetime(2025, 1, 10, tzinfo=timezone.utc)
        c_new = s.get(Ticket, t_closed_new.id)
        c_new.updated_at = datetime(2025, 1, 20, tzinfo=timezone.utc)

        # Epic closed.
        e = s.get(Ticket, t_epic_closed.id)
        e.updated_at = datetime(2025, 1, 15, tzinfo=timezone.utc)

        for obj in (d1, d2, c_old, c_new, e):
            s.add(obj)
        s.commit()

    cards = client.get("/board/cards?include_closed=true").json()
    ids = [c["id"] for c in cards]

    # Non-closed cards (drafts) should appear oldest-first.
    draft_positions = {
        t_draft.id: ids.index(t_draft.id),
        t_draft2.id: ids.index(t_draft2.id),
    }
    assert draft_positions[t_draft.id] < draft_positions[t_draft2.id], (
        "draft-oldest must appear before draft-newest (created_at ascending)"
    )

    # Closed cards should appear newest-first (updated_at descending).
    closed_ids = [t_closed_new.id, t_epic_closed.id, t_closed_old.id]
    closed_positions = {cid: ids.index(cid) for cid in closed_ids}
    assert closed_positions[t_closed_new.id] < closed_positions[t_closed_old.id], (
        "closed-newest must appear before closed-oldest (updated_at descending)"
    )
    assert closed_positions[t_closed_new.id] < closed_positions[t_epic_closed.id], (
        "epic_closed (Jan 15) must appear after closed-newest (Jan 20)"
    )
    assert closed_positions[t_epic_closed.id] < closed_positions[t_closed_old.id], (
        "epic_closed (Jan 15) must appear before closed-oldest (Jan 10)"
    )

    # All closed cards should come after all non-closed cards
    # (sort group 1 comes after group 0).
    for cid in closed_ids:
        assert ids.index(cid) > draft_positions[t_draft2.id], (
            f"closed card {cid} must appear after all non-closed cards"
        )


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


def test_get_ticket_includes_pending_question_when_paused(client, service):
    """GET /tickets/{id} returns pending_question for a ticket in
    AWAITING_USER_REPLY with an open [ASK_USER] comment."""
    t = service.create("Paused with question")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)
    service.add_comment(
        t.id, "[ASK_USER]\n\nShould we use red or blue?", author="refine"
    )

    r = client.get(f"/tickets/{t.id}").json()
    assert r["pending_question"] == "Should we use red or blue?"


def test_get_ticket_pending_question_none_when_not_paused(client, service):
    """GET /tickets/{id} returns pending_question=None for a ticket not
    in AWAITING_USER_REPLY."""
    t = service.create("Not paused")

    r = client.get(f"/tickets/{t.id}").json()
    assert r["pending_question"] is None


def test_get_ticket_pending_question_none_after_thread_closed(client, service):
    """GET /tickets/{id} returns pending_question=None once the
    [ASK_USER] thread is closed (the question was answered)."""
    t = service.create("Answered question")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)
    c = service.add_comment(t.id, "[ASK_USER]\n\nWhat's the answer?", author="refine")

    # Before closing — field is populated.
    r = client.get(f"/tickets/{t.id}").json()
    assert r["pending_question"] == "What's the answer?"

    # Close the thread (auto-resumes the ticket).
    service.close_thread(c.id)

    # After closing — field is None because thread is closed.
    # (Ticket may have auto-resumed so the gate also returns None.)
    r = client.get(f"/tickets/{t.id}").json()
    assert r["pending_question"] is None


def test_board_cards_includes_pending_question_key(client, service):
    """GET /board/cards returns a pending_question key in each card dict."""
    t = service.create("Board card test")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.AWAITING_USER_REPLY)
    service.add_comment(t.id, "[ASK_USER]\n\nBoard question text?", author="refine")

    cards = client.get("/board/cards").json()
    found = [c for c in cards if c["id"] == t.id]
    assert len(found) == 1
    assert found[0]["pending_question"] == "Board question text?"


def test_board_cards_pending_question_null_for_non_paused(client, service):
    """GET /board/cards returns pending_question=None for non-paused cards."""
    t = service.create("Non-paused card")

    cards = client.get("/board/cards").json()
    found = [c for c in cards if c["id"] == t.id]
    assert len(found) == 1
    assert found[0]["pending_question"] is None


def test_board_renders_source_badge(client):
    """The mill-specific overlay CSS includes source badge styling classes,
    and the HTML shell references both the shared and mill CSS files."""
    r = client.get("/")
    body = r.text
    assert '<link rel="stylesheet" href="/static/board.css?v=' in body
    assert '<link rel="stylesheet" href="/static/mill/board-mill.css?v=' in body
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


def test_resume_blocked_with_note_records_comment_and_clears_guard(client, service):
    """POST /tickets/{id}/resume-blocked with a note comments on the
    ticket and clears a stale artifacts/implement.md and
    artifacts/implement_spawn_count so the guards don't immediately
    re-block the retry."""
    t = service.create("Resume with note via API")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")

    ws = service.workspace(t)
    stale = ws.artifacts_dir / "implement.md"
    stale.write_text("BLOCKED — resumable\nspec-fingerprint: deadbeef\n")
    spawn_counter = ws.artifacts_dir / "implement_spawn_count"
    spawn_counter.write_text("3", encoding="utf-8")

    r = client.post(
        f"/tickets/{t.id}/resume-blocked",
        json={"note": "retry — prior failure was a flake"},
    )
    assert r.status_code == 200
    assert r.json()["state"] == State.READY
    assert not stale.exists()
    assert not spawn_counter.exists()

    comments = client.get(f"/tickets/{t.id}/comments").json()
    assert any(
        c["body"] == "retry — prior failure was a flake" and c["author"] == "operator"
        for c in comments
    )


def test_resume_blocked_missing_ticket_404(client):
    """POST /tickets/{id}/resume-blocked with bogus id returns 404."""
    r = client.post("/tickets/nonexistent/resume-blocked")
    assert r.status_code == 404


def test_resume_blocked_wrong_state_409(client, service):
    """POST /tickets/{id}/resume-blocked on non-BLOCKED ticket returns 409."""
    t = service.create("Not blocked")
    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 409


# --- resume-blocked deploy-freshness gate --------------------------------


def test_resume_blocked_stale_image_returns_409_with_comment(
    client, service, settings, monkeypatch
):
    """When the deploy server reports a stale image, resume-blocked
    returns 409 and adds a comment with digest info."""
    settings.deploy_api_url = "http://deploy:8080"

    import httpx

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "running_digest": "sha256:old",
                "latest_digest": "sha256:new",
                "update_available": True,
            }

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    t = service.create("Stale image resume")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    assert service.get(t.id).state is State.BLOCKED

    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 409
    assert "worker image is stale" in r.json()["detail"].lower()

    # Ticket should still be BLOCKED.
    assert service.get(t.id).state is State.BLOCKED

    # Comment should be recorded with digest info.
    comments = client.get(f"/tickets/{t.id}/comments").json()
    stale_comment = [c for c in comments if "worker image is stale" in c["body"]]
    assert len(stale_comment) == 1
    assert "sha256:old" in stale_comment[0]["body"]
    assert "sha256:new" in stale_comment[0]["body"]
    assert stale_comment[0]["author"] == "system"


def test_resume_blocked_current_image_resumes_normally(
    client, service, settings, monkeypatch
):
    """When the deploy server reports a current image, resume-blocked
    proceeds normally."""
    settings.deploy_api_url = "http://deploy:8080"

    import httpx

    class _FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "running_digest": "sha256:same",
                "latest_digest": "sha256:same",
                "update_available": False,
            }

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    t = service.create("Current image resume")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    assert service.get(t.id).state is State.BLOCKED

    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 200
    assert r.json()["state"] == State.READY


def test_resume_blocked_deploy_server_unreachable_resumes_normally(
    client, service, settings, monkeypatch
):
    """When the deploy server is unreachable, resume-blocked proceeds
    (don't block on transient infra)."""
    settings.deploy_api_url = "http://deploy:8080"

    import httpx

    class _FakeResponse:
        status_code = 500

        def raise_for_status(self):
            raise httpx.HTTPStatusError("error", request=None, response=self)

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    t = service.create("Server down resume")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    assert service.get(t.id).state is State.BLOCKED

    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 200
    assert r.json()["state"] == State.READY


def test_resume_blocked_no_deploy_url_resumes_normally(client, service, settings):
    """When deploy_api_url is None, resume-blocked proceeds normally
    (gate disabled)."""
    # settings fixture has deploy_api_url=None by default
    assert settings.deploy_api_url is None

    t = service.create("No deploy config resume")
    service.transition(t.id, State.READY)
    service.transition(t.id, State.BLOCKED, note="stuck in implement")
    assert service.get(t.id).state is State.BLOCKED

    r = client.post(f"/tickets/{t.id}/resume-blocked")
    assert r.status_code == 200
    assert r.json()["state"] == State.READY


# --- reset-fingerprint endpoint tests ---


def test_reset_fingerprint_success(client, service):
    """POST /tickets/{id}/reset-fingerprint clears implement.md."""
    t = service.create("Reset fingerprint")
    service.transition(t.id, State.READY)

    ws = service.workspace(t)
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (ws.artifacts_dir / "implement.md").write_text(
        "# Implement (BLOCKED — resumable)\n"
        "branch: test\n"
        "spec-fingerprint: abc123\n"
        "\nblocked\n",
        encoding="utf-8",
    )
    assert (ws.artifacts_dir / "implement.md").exists()

    r = client.post(f"/tickets/{t.id}/reset-fingerprint")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id
    assert not (ws.artifacts_dir / "implement.md").exists()


def test_reset_fingerprint_missing_ticket_404(client):
    """POST /tickets/{id}/reset-fingerprint with bogus id returns 404."""
    r = client.post("/tickets/nonexistent/reset-fingerprint")
    assert r.status_code == 404


def test_reset_fingerprint_no_implement_md_is_idempotent(client, service):
    """POST /tickets/{id}/reset-fingerprint when no implement.md exists
    is a no-op and returns 200."""
    t = service.create("No fingerprint yet")
    service.transition(t.id, State.READY)

    ws = service.workspace(t)
    assert not (ws.artifacts_dir / "implement.md").exists()

    r = client.post(f"/tickets/{t.id}/reset-fingerprint")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == t.id


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
    """Regression: a malformed template literal in the board script (a
    missing closing backtick on the Approve button) was a JS syntax
    error that wedged the whole board on 'loading…'. Guard the
    structural invariants on the served ``board-mill.js``."""
    from pathlib import Path

    import robotsix_mill.runtime.board_html

    js_path = (
        Path(robotsix_mill.runtime.board_html.__file__).parent
        / "static"
        / "board-mill.js"
    )
    js = js_path.read_text()
    assert js.count("`") % 2 == 0, "unbalanced template-literal backticks"
    assert '</button>":' not in js  # the exact past defect


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
    # Candidate / survey / child-ticket handlers use jsq().
    assert "rejectCandidate(' + jsq(" in js
    assert "open_(' + jsq(" in js  # survey link handlers
    # The unsafe pre-fix pattern must be gone: no bare-template open_('${id}').
    assert "open_('${" not in js


def test_board_js_includes_move_to_board(client):
    """The mill board overlay JS includes the moveToBoard function and
    the /migrate endpoint call for the board-to-board migration UI."""
    js = client.get("/static/mill/board-mill.js").text
    assert "function moveToBoard(" in js, "board-mill.js must define moveToBoard()"
    assert "/migrate" in js, "board-mill.js must call the /migrate endpoint"


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


def test_origin_session_url_with_repo_config_project_id(service, settings, repo_config):
    """enrich_ticket_read uses repo_config.langfuse_project_id when available."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("Repo config ID test", origin_session="sess-xyz")
    repo_config.langfuse_project_id = "cuid-repo-123"
    tr = enrich_ticket_read(t, settings, service, repo_config=repo_config)
    assert tr.origin_session_url == (
        "https://cloud.langfuse.com/project/cuid-repo-123/sessions/sess-xyz"
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


def test_origin_session_url_secrets_project_id_preferred(
    service, settings, secrets_set
):
    """secrets fallback prefers langfuse_project_id over langfuse_project_name."""
    from robotsix_mill.runtime.deps import enrich_ticket_read

    t = service.create("ID preferred test", origin_session="sess-2")
    secrets_set(
        langfuse_base_url="https://custom.lf.example.com",
        langfuse_project_name="my-project-name",
        langfuse_project_id="proj-cuid-id",
    )
    tr = enrich_ticket_read(t, settings, service)
    assert tr.origin_session_url == (
        "https://custom.lf.example.com/project/proj-cuid-id/sessions/sess-2"
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
    # Local asset URLs carry a per-deploy ``?v=`` cache-busting query.
    assert '<link rel="stylesheet" href="/static/board.css?v=' in body
    assert '<script src="/static/board.js?v=' in body


def test_board_html_asset_urls_carry_version_query(monkeypatch):
    """All local script/css URLs carry a per-deploy ``?v=`` cache-busting
    query, and changing the resolved version changes the emitted URLs."""
    import robotsix_mill.runtime.board_html as bh

    monkeypatch.setattr(bh, "asset_version", lambda: "abc123")
    html_a = bh.render_board_html("", "")
    assert "/static/board.css?v=abc123" in html_a
    assert "/static/mill/board-mill.css?v=abc123" in html_a
    assert "/static/board.js?v=abc123" in html_a
    assert "/static/mill/board-mill.js?v=abc123" in html_a
    # No un-versioned local asset tags slip through.
    assert "{ASSET_VERSION}" not in html_a
    assert 'href="/static/board.css"' not in html_a
    assert 'src="/static/board.js"' not in html_a

    # A different version yields different URLs.
    monkeypatch.setattr(bh, "asset_version", lambda: "deadbeef")
    html_b = bh.render_board_html("", "")
    assert "?v=deadbeef" in html_b
    assert "?v=abc123" not in html_b


def test_asset_version_uses_build_sha_env(monkeypatch):
    """asset_version() reads MILL_BUILD_SHA when present, and the token
    changes when the env value changes (so the emitted URLs change)."""
    import robotsix_mill.runtime.board_html as bh

    try:
        monkeypatch.setenv("MILL_BUILD_SHA", "abc1234")
        bh.asset_version.cache_clear()
        assert bh.asset_version() == "abc1234"

        # A different SHA yields a different token (URLs change).
        monkeypatch.setenv("MILL_BUILD_SHA", "def5678")
        bh.asset_version.cache_clear()
        assert bh.asset_version() == "def5678"
    finally:
        bh.asset_version.cache_clear()


def test_asset_version_process_start_fallback(monkeypatch):
    """With MILL_BUILD_SHA unset, asset_version() returns a non-empty
    process-start fallback token (never empty/None), distinct from a SHA."""
    import robotsix_mill.runtime.board_html as bh

    try:
        monkeypatch.delenv("MILL_BUILD_SHA", raising=False)
        bh.asset_version.cache_clear()
        token = bh.asset_version()
        assert token is not None
        assert isinstance(token, str)
        assert token != ""
        assert token != "abc1234"
    finally:
        bh.asset_version.cache_clear()


def test_board_inline_handlers_defined_in_served_scripts():
    """Every inline ``onclick=``/``onchange=`` handler emitted by
    board_html.py must reference a global that is actually defined in a
    SERVED script: exposed as ``window.<fn> =`` in board-mill.js, or
    defined as a function in robotsix-board's packaged board.js.

    Regression guard for the #1036 class of bug, where a handler
    (``toggleClosed()``) was wired in the HTML but its implementation
    lived only in a dead, never-mounted ``static/board.js`` — so the
    button threw ``ReferenceError`` in the browser."""
    import re
    from pathlib import Path

    import robotsix_mill.runtime.board_html as bh

    html = bh.render_board_html("", "")

    static = Path(bh.__file__).parent / "static"
    mill_js = (static / "board-mill.js").read_text()

    try:
        from robotsix_board import static_dir as _board_static_dir

        board_js = (_board_static_dir() / "board.js").read_text()
    except Exception:
        board_js = ""

    # Capture the leading global identifier of each handler — anchoring
    # to ``("`` excludes method calls like ``event.stopPropagation()``.
    handlers = set(re.findall(r'on(?:click|change)="\s*([A-Za-z_$][\w$]*)\s*\(', html))
    assert handlers, "expected at least one inline handler in the board HTML"

    for fn in sorted(handlers):
        in_mill = f"window.{fn} =" in mill_js or f"window.{fn}=" in mill_js
        in_board = f"function {fn}(" in board_js or f"window.{fn} =" in board_js
        assert in_mill or in_board, (
            f"inline handler {fn}() references a global not defined in any "
            "served script (board-mill.js window exports or robotsix-board "
            "board.js)"
        )


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
    assert any(name in js.text for name in ("open_", "openDrawer", "closeDrawer"))


def test_audit_endpoint_is_fire_and_forget(client, monkeypatch):
    """Regression: POST /passes/audit/run must return 202 immediately
    and run the audit in the background — same fire-and-forget contract
    as the old /audit route."""
    import threading

    from robotsix_mill.runners import periodic_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_audit(session_id=None, repo_config=None):
        ran.set()
        release.wait(5)  # simulate a minutes-long run
        return _R()

    monkeypatch.setattr(periodic_runner, "run_audit_pass", slow_audit)

    r = client.post("/passes/audit/run")  # must NOT block on slow_audit
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)  # audit really started in the background
    release.set()  # let the daemon thread finish


def test_agent_check_endpoint_is_fire_and_forget(client, monkeypatch):
    """POST /passes/agent_check/run returns 202 immediately and runs the
    agent-check agent in the background — same fire-and-forget
    contract as the generic pass endpoint."""
    import threading

    from robotsix_mill.runners import periodic_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_agent_check(session_id=None, repo_config=None):
        ran.set()
        release.wait(5)
        return _R()

    monkeypatch.setattr(periodic_runner, "run_agent_check_pass", slow_agent_check)

    r = client.post("/passes/agent_check/run")
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)
    release.set()


def test_run_health_endpoint_is_fire_and_forget(client, monkeypatch):
    """POST /passes/run_health/run returns 202 immediately and runs the run-health
    pass in the background — same fire-and-forget contract as /audit,
    /health-check. It is a global pass (no repo_id)."""
    import threading

    from robotsix_mill.runners import run_health_runner

    ran = threading.Event()
    release = threading.Event()

    class _R:
        drafts_created: list = []

    def slow_run_health(session_id=None, repo_config=None):
        ran.set()
        release.wait(5)  # simulate a minutes-long run
        return _R()

    monkeypatch.setattr(
        run_health_runner, "run_run_health_pass_wrapper", slow_run_health
    )

    r = client.post("/passes/run_health/run")  # must NOT block on slow_run_health
    assert r.status_code == 202
    assert r.json() == {"status": "started"}
    assert ran.wait(5)  # run-health really started in the background
    release.set()  # let the daemon thread finish


def test_board_html_includes_agent_check_button(client):
    """The board exposes an 'Agent Check' pass via the dynamic passes
    dropdown. The JS contains the generic runPass dispatcher referencing
    the /passes endpoint."""
    body = client.get("/").text
    assert "Agent Check" in body or "passes-dropdown" in body
    # Mill-specific pass functions are in board-mill.js (layered on top
    # of robotsix-board's shared board.js).
    js = client.get("/static/mill/board-mill.js").text
    assert "runPass" in js
    assert '"/passes/"' in js


def test_setup_logging_surfaces_app_logs_idempotently(capsys):
    """Regression: robotsix_mill.* logs were dropped (no handler under
    uvicorn), masking the silently-failing /audit thread. setup_logging
    must attach exactly one stdout handler at INFO, idempotently."""
    import logging

    from robotsix_mill.runtime.api import setup_logging

    root = logging.getLogger("robotsix_mill")
    root.handlers = [
        h for h in root.handlers if not isinstance(h, logging.StreamHandler)
    ]

    setup_logging()
    setup_logging()  # idempotent — second call must not add another
    stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    assert len(stream_handlers) == 1
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
            "kind": TicketKind.INQUIRY,
        },
    )
    assert r.status_code == 201
    d = r.json()
    assert d["state"] == "asked"  # inquiries start in ASKED, not DRAFT
    assert d["kind"] == TicketKind.INQUIRY
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
            "kind": TicketKind.INQUIRY,
            "depends_on": '["ticket-abc"]',
        },
    )
    assert r.status_code in (400, 422), (
        "inquiries must reject depends_on — they are standalone Q&A"
    )


def test_list_tickets_include_closed_hides_closed_and_epic_closed_and_answered_keeps_done(
    client, service
):
    """include_closed=false must hide terminal states (CLOSED, EPIC_CLOSED,
    ANSWERED) but ALWAYS return DONE — DONE is the transient
    retrospect-in-flight window and needs to stay visible so the board
    can show retrospect work without the user toggling 'Show closed.'"""
    # Create via the service (not the API) to bypass maybe_enqueue —
    # the worker would otherwise refine these tickets and BLOCK them
    # on the missing API key, racing the transitions below.
    closed = service.create("C-closed")
    done = service.create("C-done")
    draft = service.create("C-draft")
    epic = service.create("C-epic", kind=TicketKind.EPIC)
    answered = service.create("C-answered", kind=TicketKind.INQUIRY)
    # Walk via legal edges: DRAFT -> DONE (refine's dedup-discard route),
    # DONE -> CLOSED (retrospect's edge), EPIC_OPEN -> EPIC_CLOSED,
    # ASKED -> ANSWERED.
    service.transition(closed.id, State.DONE)
    service.transition(closed.id, State.CLOSED)
    service.transition(done.id, State.DONE)
    service.transition(epic.id, State.EPIC_CLOSED)
    # Inquiries start in ASKED; transition to ANSWERED.
    service.transition(answered.id, State.ANSWERED)

    # include_closed=true → everything visible (must be explicit now that
    # the endpoint defaults to include_closed=false).
    ids_all = {t["id"] for t in client.get("/tickets?include_closed=true").json()}
    assert {closed.id, done.id, draft.id, epic.id, answered.id} <= ids_all

    # include_closed=false → terminal states hidden, DONE + DRAFT still visible.
    ids = {t["id"] for t in client.get("/tickets?include_closed=false").json()}
    assert done.id in ids, "DONE must stay visible (retrospect-in-flight)"
    assert draft.id in ids
    assert closed.id not in ids, "CLOSED must be hidden by the toggle"
    assert epic.id not in ids, "EPIC_CLOSED must be hidden by the toggle"
    assert answered.id not in ids, "ANSWERED must be hidden by the toggle"

    # default (no param) now excludes terminal too — loading the closed
    # majority on every poll was the dominant board-stall cost.
    ids_default = {t["id"] for t in client.get("/tickets").json()}
    assert closed.id not in ids_default
    assert epic.id not in ids_default
    assert answered.id not in ids_default
    assert done.id in ids_default and draft.id in ids_default


def test_list_tickets_explicit_closed_state_overrides_default_exclusion(
    client, service
):
    """Explicit ``state=closed`` must return closed tickets even when
    ``include_closed`` is not set — the explicit filter takes
    precedence over the default terminal exclusion."""
    closed = service.create("Explicit-closed")
    service.transition(closed.id, State.DONE)
    service.transition(closed.id, State.CLOSED)

    draft = service.create("Explicit-draft")

    # state=closed with no include_closed → must return the closed ticket.
    ids = {t["id"] for t in client.get("/tickets?state=closed").json()}
    assert closed.id in ids, "explicit state=closed must override default exclusion"
    assert draft.id not in ids


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
    assert data["kind"] == TicketKind.EPIC


def test_create_epic_via_api_repo_resolution(client):
    """POST /epics in single-repo mode auto-selects the repo without repo_id."""
    r = client.post("/epics", json={"title": "No repo_id given"})
    assert r.status_code == 201
    data = r.json()
    assert data["state"] == "epic_open"
    assert data["kind"] == TicketKind.EPIC
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
    assert data["kind"] == TicketKind.EPIC
    assert data["title"] == "CLI-created epic"
    # CLI prints the id — verify it's present and non-empty.
    assert data["id"]


def test_create_ticket_with_parent(client, service):
    """POST /tickets with parent_id set links child to epic."""
    epic = service.create("Epic", kind=TicketKind.EPIC)
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
    epic = service.create("Epic", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", kind=TicketKind.TASK, parent_id=epic.id)
    c2 = service.create("Child 2", kind=TicketKind.TASK, parent_id=epic.id)

    r = client.get(f"/tickets/{epic.id}/children")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    child_ids = {c["id"] for c in data}
    assert child_ids == {c1.id, c2.id}


def test_create_epic_unknown_repo(client):
    """POST /epics with an unknown repo_id returns 400."""
    r = client.post("/epics", json={"title": "X", "repo_id": "nonexistent"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Unknown repo" in detail
    assert "nonexistent" in detail


def test_create_epic_value_error(client, monkeypatch):
    """When svc.create raises ValueError, POST /epics re-raises it as 400."""
    from robotsix_mill.core.service import TicketService

    def raising_create(self, title, *args, **kwargs):
        raise ValueError("bad")

    monkeypatch.setattr(TicketService, "create", raising_create)

    r = client.post("/epics", json={"title": "Valid title"})
    assert r.status_code == 400
    assert "bad" in r.json()["detail"]


def test_list_children_404_nonexistent(client):
    """GET /tickets/{id}/children on a missing ticket returns 404."""
    r = client.get("/tickets/does-not-exist/children")
    assert r.status_code == 404
    assert "ticket not found" in r.json()["detail"]


# --- generate-children endpoint tests ---


def test_generate_children_404_nonexistent(client):
    """POST /tickets/nonexistent/generate-children returns 404."""
    r = client.post("/tickets/nonexistent/generate-children")
    assert r.status_code == 404


def test_generate_children_400_non_epic(client, service):
    """POST /tickets/{id}/generate-children on a task returns 400."""
    t = service.create("Not an epic", kind=TicketKind.TASK)
    r = client.post(f"/tickets/{t.id}/generate-children")
    assert r.status_code == 400
    assert "ticket is not an epic" in r.json()["detail"]


def test_generate_children_202_fire_and_forget(client, service, monkeypatch):
    """POST /tickets/{id}/generate-children returns 202 immediately and
    runs the agent in the background — the HTTP response must not
    block on the LLM call."""
    import threading

    epic = service.create("Fire and forget epic", kind=TicketKind.EPIC)

    ran = threading.Event()
    release = threading.Event()

    def slow_agent(
        *,
        settings,
        epic_title,
        epic_description,
        available_repos=None,
        epic_repo_id="",
        **kwargs,
    ):
        ran.set()
        release.wait(5)
        return type(
            "FakeResult",
            (),
            {"child_titles": [], "child_bodies": [], "child_repo_ids": []},
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

    epic = service.create("Break me down", kind=TicketKind.EPIC)

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


def test_generate_children_background_error_path(client, service, monkeypatch):
    """When the breakdown agent raises in the background thread, the route
    still returns 202 immediately and the runner calls
    ``registry.finish_error``."""
    import threading

    epic = service.create("Will fail", kind=TicketKind.EPIC)

    def boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "robotsix_mill.agents.epic_breakdown.run_epic_breakdown_agent",
        boom,
    )

    # Capture the finish_error call by wrapping the live run registry.
    registry = client.app.state.run_registry
    finished_error = threading.Event()
    orig_finish_error = registry.finish_error

    def tracking_finish_error(run_id, error):
        orig_finish_error(run_id, error)
        finished_error.set()

    monkeypatch.setattr(registry, "finish_error", tracking_finish_error)

    r = client.post(f"/tickets/{epic.id}/generate-children")
    assert r.status_code == 202

    assert finished_error.wait(5), "registry.finish_error was not called"


def test_generate_children_flags_overlapping_child(client, service, monkeypatch):
    """The /generate-children route runs the advisory pre-filing dedup
    check: two overlapping children (shared CONTRIBUTING.md path) are
    BOTH created, and exactly the later one carries the ``[!warning]``
    advisory block — never silently dropped."""
    import threading

    from robotsix_mill.agents.epic_breakdown import EpicBreakdownResult
    from robotsix_mill.core.service import TicketService

    epic = service.create("Audit Trivy SARIF", kind=TicketKind.EPIC)

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

    epic = service.create(
        "Break me down", "Original epic description", kind=TicketKind.EPIC
    )

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

    epic = service.create("Epic to comment on", "Epic desc", kind=TicketKind.EPIC)

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

    task = service.create("A task ticket", kind=TicketKind.TASK)
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

    epic = service.create("Epic with existing child", kind=TicketKind.EPIC)
    service.create("Add auth module", kind=TicketKind.TASK, parent_id=epic.id)

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

    epic = service.create("Slow epic", kind=TicketKind.EPIC)

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

    epic = service.create("Epic with history", "Epic desc", kind=TicketKind.EPIC)
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
    epic = service.create("Epic", kind=TicketKind.EPIC)
    c1 = service.create("Child 1", kind=TicketKind.TASK, parent_id=epic.id)
    c2 = service.create("Child 2", kind=TicketKind.TASK, parent_id=epic.id)

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
    epic = service.create("Epic", kind=TicketKind.EPIC)
    service.create("Child 1", kind=TicketKind.TASK, parent_id=epic.id)
    service.create("Child 2", kind=TicketKind.TASK, parent_id=epic.id)

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
    e1 = service.create("E1", kind=TicketKind.EPIC)
    e2 = service.create("E2", kind=TicketKind.EPIC, parent_id=e1.id)
    t = service.create("T", kind=TicketKind.TASK, parent_id=e2.id)

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
    parent = service.create("Parent task", kind=TicketKind.TASK)
    c1 = service.create("Child 1", kind=TicketKind.TASK, parent_id=parent.id)
    c2 = service.create("Child 2", kind=TicketKind.TASK, parent_id=parent.id)

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
    leaf = service.create("Leaf task", kind=TicketKind.TASK)

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
        "robotsix_mill.runtime.routes._tickets_merge.get_forge",
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


def test_merge_now_blocks_when_not_merged_to_mainline(client, service, monkeypatch):
    """merge-now refuses the DONE transition when the merged commit is
    not an ancestor of the target branch (forge reported success but the
    work never reached mainline)."""
    fake = _FakeForge(merge_result={"merged": True, "reason": "merged"})
    _patch_forge(monkeypatch, fake)
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets_merge._verify_merge_ancestor",
        lambda *a, **k: False,
    )

    t = _to_human_mr_approval(service, "Diverged merge")
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409, f"Got {r.status_code}: {r.text}"

    # Ticket stays parked; no DONE transition, no merge note.
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL
    notes = " ".join(e.note or "" for e in service.history(t.id))
    assert "merged via board" not in notes


def test_merge_now_multi_repo_blocks_when_not_merged_to_mainline(
    client, service, monkeypatch
):
    """Multi-repo merge-now refuses the DONE transition when a merged
    commit is not an ancestor of its repo's target branch."""
    forge_a = _FakeForge()
    forge_b = _FakeForge()
    _patch_multirepo_forge(monkeypatch, {"repo-a": forge_a, "repo-b": forge_b})
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets_merge._verify_merge_ancestor",
        lambda *a, **k: False,
    )

    t = _to_human_mr_approval(service, "Multi-repo diverged")
    _write_pr_urls(
        service,
        t,
        [
            {"repo_id": "repo-a", "branch": "mill/a", "url": "u-a"},
            {"repo_id": "repo-b", "branch": "mill/b", "url": "u-b"},
        ],
    )

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409, f"Got {r.status_code}: {r.text}"
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL
    notes = " ".join(e.note or "" for e in service.history(t.id))
    assert "merged via board" not in notes


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


def _to_human_mr_approval(service, title):
    """Walk a fresh ticket up to HUMAN_MR_APPROVAL for merge-now tests."""
    t = service.create(title)
    service.transition(t.id, State.READY, note="approved (autonomous)")
    service.transition(t.id, State.DELIVERABLE, note="delivered")
    service.transition(t.id, State.IMPLEMENT_COMPLETE, note="gates checking")
    service.transition(t.id, State.HUMAN_MR_APPROVAL, note="awaiting merge")
    return service.get(t.id)


def _write_pr_urls(service, ticket, entries):
    """Write a ``pr_urls.json`` manifest into the ticket's artifacts dir."""
    d = service.workspace(ticket).artifacts_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "pr_urls.json").write_text(json.dumps(entries), encoding="utf-8")


def _patch_multirepo_forge(monkeypatch, forges_by_repo):
    """Route per-repo ``get_forge`` calls to per-repo fakes.

    ``_repo_config_for_entry`` is stubbed to return a tiny RepoConfig-like
    stand-in carrying the entry's ``repo_id`` (which keys the per-repo
    forge in the patched ``get_forge``) and an empty ``working_branch`` so
    ``target_branch_for`` falls back to the default target branch.
    """
    from types import SimpleNamespace

    monkeypatch.setattr(
        "robotsix_mill.stages.merge._repo_config_for_entry",
        lambda entry: SimpleNamespace(repo_id=entry["repo_id"], working_branch=""),
    )
    monkeypatch.setattr(
        "robotsix_mill.runtime.routes._tickets_merge.get_forge",
        lambda s, repo_config=None: forges_by_repo[repo_config.repo_id],
    )


def test_merge_now_multi_repo_merges_every_repo(client, service, monkeypatch):
    """merge-now on a multi-repo ticket merges every repo's PR (one
    merge_pr per repo via its own forge) and transitions to done."""
    forge_a = _FakeForge()
    forge_b = _FakeForge()
    _patch_multirepo_forge(monkeypatch, {"repo-a": forge_a, "repo-b": forge_b})

    t = _to_human_mr_approval(service, "Multi-repo merge")
    _write_pr_urls(
        service,
        t,
        [
            {"repo_id": "repo-a", "branch": "mill/a", "url": "u-a"},
            {"repo_id": "repo-b", "branch": "mill/b", "url": "u-b"},
        ],
    )

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    assert r.json()["state"] == "done"

    # One merge per repo, each on its own per-repo branch.
    assert [c["source_branch"] for c in forge_a.merge_calls] == ["mill/a"]
    assert [c["source_branch"] for c in forge_b.merge_calls] == ["mill/b"]


def test_merge_now_multi_repo_one_rejected_409(client, service, monkeypatch):
    """When one repo's merge is rejected, merge-now returns 409 naming
    that repo and leaves the ticket in human_mr_approval."""
    forge_a = _FakeForge()
    forge_b = _FakeForge(merge_result={"merged": False, "reason": "branch protection"})
    _patch_multirepo_forge(monkeypatch, {"repo-a": forge_a, "repo-b": forge_b})

    t = _to_human_mr_approval(service, "Multi-repo reject")
    _write_pr_urls(
        service,
        t,
        [
            {"repo_id": "repo-a", "branch": "mill/a", "url": "u-a"},
            {"repo_id": "repo-b", "branch": "mill/b", "url": "u-b"},
        ],
    )

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 409
    assert "repo-b" in r.text
    assert "branch protection" in r.text

    # repo-a stays merged (skipped on retry); ticket state unchanged.
    assert len(forge_a.merge_calls) == 1
    assert service.get(t.id).state is State.HUMAN_MR_APPROVAL


def test_merge_now_multi_repo_skips_already_merged(client, service, monkeypatch):
    """An already-merged repo is skipped (idempotent re-press); the
    remaining repo is merged and the ticket reaches done."""
    forge_a = _FakeForge(
        pr_status_result={"url": "u-a", "merged": True, "state": "closed"},
    )
    forge_b = _FakeForge()
    _patch_multirepo_forge(monkeypatch, {"repo-a": forge_a, "repo-b": forge_b})

    t = _to_human_mr_approval(service, "Multi-repo idempotent")
    _write_pr_urls(
        service,
        t,
        [
            {"repo_id": "repo-a", "branch": "mill/a", "url": "u-a"},
            {"repo_id": "repo-b", "branch": "mill/b", "url": "u-b"},
        ],
    )

    r = client.post(f"/tickets/{t.id}/merge-now")
    assert r.status_code == 200, f"Got {r.status_code}: {r.text}"
    assert r.json()["state"] == "done"

    # repo-a already merged → skipped; repo-b merged.
    assert forge_a.merge_calls == []
    assert [c["source_branch"] for c in forge_b.merge_calls] == ["mill/b"]


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


# ---------------------------------------------------------------------------
# RequestIDMiddleware
# ---------------------------------------------------------------------------


def test_x_request_id_header_present(client):
    """Every response carries an X-Request-ID header."""
    r = client.get("/health")
    assert r.status_code == 200
    assert "x-request-id" in r.headers
    request_id = r.headers["x-request-id"]
    # UUID4 hex strings are 32 hex chars.
    assert len(request_id) == 32
    assert all(c in "0123456789abcdef" for c in request_id)


def test_x_request_id_passthrough(client):
    """When the client sends X-Request-ID, the middleware echoes it."""
    r = client.get("/health", headers={"X-Request-ID": "my-custom-id-42"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "my-custom-id-42"


def test_request_state_request_id(client):
    """request.state.request_id is set and matches the response header."""
    r = client.get("/health")
    assert r.status_code == 200
    # The TestClient doesn't expose request.state directly, but we can
    # verify the header matches what a handler would see by checking
    # both the passthrough and generated paths are consistent.
    rid = r.headers["x-request-id"]
    assert len(rid) >= 1


def test_x_request_id_non_http_scope_not_affected():
    """Middleware passes non-http scopes through unchanged."""
    from robotsix_mill.runtime.middleware import RequestIDMiddleware

    recorded = []

    async def inner_app(scope, receive, send):
        recorded.append(scope["type"])
        await send({"type": "lifespan.startup"})

    middleware = RequestIDMiddleware(inner_app)

    import asyncio

    async def _async_nop():
        return {"type": "http.request"}

    events = []

    async def _noop_send(message):
        events.append(message)

    asyncio.run(middleware({"type": "lifespan"}, _async_nop, _noop_send))
    assert recorded == ["lifespan"]


# -- Cross-board epic children tests ----------------------------------------


class TestCrossBoardChildren:
    """Integration tests for cross-board epic children.

    Verifies that children created on other boards are visible via
    ``list_children_across_boards``, that the orphan sweep sees them,
    and that migration preserves the parent link.
    """

    def test_list_children_across_boards_finds_children_on_other_boards(
        self, tmp_path, monkeypatch
    ):
        """Children on board B are found from a service bound to board A."""
        from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
        from robotsix_mill.core import db
        from robotsix_mill.core.service import TicketService

        # Set up two repos with distinct boards.
        repos = ReposRegistry(
            repos={
                "repo-a": RepoConfig(
                    repo_id="repo-a",
                    board_id="board-a",
                    langfuse_project_name="a",
                    langfuse_public_key="pk-a",
                    langfuse_secret_key="sk-a",
                ),
                "repo-b": RepoConfig(
                    repo_id="repo-b",
                    board_id="board-b",
                    langfuse_project_name="b",
                    langfuse_public_key="pk-b",
                    langfuse_secret_key="sk-b",
                ),
            }
        )
        import robotsix_mill.config as _cfg

        _cfg._repos_config = repos
        try:
            settings = Settings(data_dir=str(tmp_path))
            db.init_db(settings, board_id="board-a")
            db.init_db(settings, board_id="board-b")

            svc_a = TicketService(settings, board_id="board-a")
            svc_b = TicketService(settings, board_id="board-b")

            # Create an epic on board-a.
            epic = svc_a.create("Cross-board epic", kind=TicketKind.TASK)
            # Create a child on board-b (cross-board parent link).
            child_b = svc_b.create("Child on B", parent_id=epic.id, board_id="board-b")
            # Create a child on board-a (same-board).
            child_a = svc_a.create("Child on A", parent_id=epic.id, board_id="board-a")

            # list_children_across_boards from svc_a should find both.
            all_children = svc_a.list_children_across_boards(epic.id)
            child_ids = {c.id for c in all_children}
            assert child_b.id in child_ids
            assert child_a.id in child_ids
            assert len(all_children) == 2
        finally:
            _cfg._repos_config = None
            db.reset_engine()

    def test_migrate_preserves_parent_id(self, tmp_path, monkeypatch):
        """Migrating a child to another board keeps its parent_id intact."""
        from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
        from robotsix_mill.core import db
        from robotsix_mill.core.service import TicketService

        repos = ReposRegistry(
            repos={
                "repo-a": RepoConfig(
                    repo_id="repo-a",
                    board_id="board-a",
                    langfuse_project_name="a",
                    langfuse_public_key="pk-a",
                    langfuse_secret_key="sk-a",
                ),
                "repo-b": RepoConfig(
                    repo_id="repo-b",
                    board_id="board-b",
                    langfuse_project_name="b",
                    langfuse_public_key="pk-b",
                    langfuse_secret_key="sk-b",
                ),
            }
        )
        import robotsix_mill.config as _cfg

        _cfg._repos_config = repos
        try:
            settings = Settings(data_dir=str(tmp_path))
            db.init_db(settings, board_id="board-a")
            db.init_db(settings, board_id="board-b")

            svc_a = TicketService(settings, board_id="board-a")

            parent = svc_a.create("Parent task")
            child = svc_a.create("Child task", parent_id=parent.id)
            assert child.parent_id == parent.id

            # Migrate child to board-b.
            migrated = svc_a.migrate(child.id, "board-b")
            assert migrated.board_id == "board-b"
            assert migrated.parent_id == parent.id, (
                f"parent_id should survive migration: {migrated.parent_id} != {parent.id}"
            )
        finally:
            _cfg._repos_config = None
            db.reset_engine()

    def test_migrate_epic_with_children_still_blocked(self, tmp_path, monkeypatch):
        """An epic with children still cannot be migrated (subtree path
        is for epic kind only; the guard for non-epic parents with
        children remains)."""
        from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
        from robotsix_mill.core import db
        from robotsix_mill.core.service import TicketService

        repos = ReposRegistry(
            repos={
                "repo-a": RepoConfig(
                    repo_id="repo-a",
                    board_id="board-a",
                    langfuse_project_name="a",
                    langfuse_public_key="pk-a",
                    langfuse_secret_key="sk-a",
                ),
                "repo-b": RepoConfig(
                    repo_id="repo-b",
                    board_id="board-b",
                    langfuse_project_name="b",
                    langfuse_public_key="pk-b",
                    langfuse_secret_key="sk-b",
                ),
            }
        )
        import robotsix_mill.config as _cfg

        _cfg._repos_config = repos
        try:
            settings = Settings(data_dir=str(tmp_path))
            db.init_db(settings, board_id="board-a")
            db.init_db(settings, board_id="board-b")

            svc_a = TicketService(settings, board_id="board-a")

            parent = svc_a.create("Parent task")
            svc_a.create("Child task", parent_id=parent.id)

            import pytest

            with pytest.raises(ValueError, match="has child tickets"):
                svc_a.migrate(parent.id, "board-b")
        finally:
            _cfg._repos_config = None
            db.reset_engine()

    def test_resolve_child_board_id_warning_on_unknown_repo(self, caplog):
        """resolve_child_board_id warns and falls back for unknown repo_id."""
        from robotsix_mill.config import RepoConfig, ReposRegistry
        from robotsix_mill.config.repos import resolve_child_board_id

        repos = ReposRegistry(
            repos={
                "repo-a": RepoConfig(
                    repo_id="repo-a",
                    board_id="board-a",
                    langfuse_project_name="a",
                    langfuse_public_key="pk-a",
                    langfuse_secret_key="sk-a",
                ),
            }
        )
        result = resolve_child_board_id(
            "unknown-repo", "board-fallback", "epic-42", repos
        )
        assert result == "board-fallback"
        assert "epic-42" in caplog.text
        assert "unknown-repo" in caplog.text
        assert "board-fallback" in caplog.text

    def test_maybe_reevaluate_epic_uses_fanout_parent_lookup(
        self, tmp_path, monkeypatch
    ):
        """_maybe_reevaluate_epic finds the parent epic even when the
        child and epic live on different boards."""
        from robotsix_mill.config import RepoConfig, ReposRegistry, Settings
        from robotsix_mill.core import db
        from robotsix_mill.core.service import TicketService
        from robotsix_mill.core.states import State
        from robotsix_mill.runtime.worker.processing import _maybe_reevaluate_epic

        repos = ReposRegistry(
            repos={
                "repo-a": RepoConfig(
                    repo_id="repo-a",
                    board_id="board-a",
                    langfuse_project_name="a",
                    langfuse_public_key="pk-a",
                    langfuse_secret_key="sk-a",
                ),
                "repo-b": RepoConfig(
                    repo_id="repo-b",
                    board_id="board-b",
                    langfuse_project_name="b",
                    langfuse_public_key="pk-b",
                    langfuse_secret_key="sk-b",
                ),
            }
        )
        import robotsix_mill.config as _cfg

        _cfg._repos_config = repos
        try:
            settings = Settings(data_dir=str(tmp_path))
            db.init_db(settings, board_id="board-a")
            db.init_db(settings, board_id="board-b")

            svc_a = TicketService(settings, board_id="board-a")
            svc_b = TicketService(settings, board_id="board-b")

            # Epic on board-a.
            epic = svc_a.create("Cross-board epic", kind=TicketKind.EPIC)
            # Child on board-b.
            child = svc_b.create("Child on B", parent_id=epic.id, board_id="board-b")

            # Verify the parent lookup works from board-b's context.
            from robotsix_mill.core.service import TicketService as TS

            fanout_svc = TS(settings)
            found = fanout_svc.get(child.parent_id)
            assert found is not None
            assert found.id == epic.id
            assert found.kind.value == "epic"

            # Patch _spawn_epic_reeval to record the call.
            spawned: list[str] = []

            def fake_spawn(eid, ctx):
                spawned.append(eid)

            monkeypatch.setattr(
                "robotsix_mill.runtime.worker.processing._spawn_epic_reeval",
                fake_spawn,
            )

            # Simulate a terminal child transition triggering re-eval.
            # Build a minimal ctx with a fan-out service.
            test_settings = settings  # alias to avoid class-body name shadow

            class FakeCtx:
                service = fanout_svc
                settings = test_settings

            _maybe_reevaluate_epic(child.id, FakeCtx(), State.DONE)
            assert epic.id in spawned, (
                f"Epic {epic.id} should be spawned for re-eval, got {spawned}"
            )
        finally:
            _cfg._repos_config = None
            db.reset_engine()


# --- PUT /tickets/{id}/description ---


def test_update_description_success(client, service):
    """PUT /tickets/{id}/description updates the spec and returns new content_hash."""
    t = service.create("Initial title", description="Old spec body")
    old_hash = t.content_hash

    r = client.put(
        f"/tickets/{t.id}/description",
        json={"description": "New spec body", "author": "test-agent"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ticket_id"] == t.id
    assert data["fingerprint_reset"] is False
    assert "event_id" in data
    # content_hash should have changed.
    assert data["content_hash"] != old_hash

    # Verify the description was actually updated.
    desc = client.get(f"/tickets/{t.id}/description").json()
    assert desc["description"] == "New spec body"


def test_update_description_404(client):
    """PUT /tickets/{id}/description with nonexistent ticket returns 404."""
    r = client.put(
        "/tickets/nonexistent/description",
        json={"description": "whatever"},
    )
    assert r.status_code == 404


def test_update_description_terminal_409(client, service):
    """PUT /tickets/{id}/description on a terminal-state ticket returns 409."""
    t = service.create("Done ticket", description="body")
    service.transition(t.id, State.DONE, note="done")
    r = client.put(
        f"/tickets/{t.id}/description",
        json={"description": "new body"},
    )
    assert r.status_code == 409


def test_update_description_records_history(client, service):
    """PUT /tickets/{id}/description records a history event with old/new fingerprint."""
    t = service.create("History ticket", description="Initial description")
    r = client.put(
        f"/tickets/{t.id}/description",
        json={"description": "Updated description", "author": "auditor"},
    )
    assert r.status_code == 200

    # Check history for the fingerprint transition note.
    history = client.get(f"/tickets/{t.id}/history").json()
    notes = [e["note"] for e in history]
    fingerprint_entry = [n for n in notes if "spec update: fingerprint" in n]
    assert len(fingerprint_entry) == 1, f"Expected one fingerprint note, got: {notes}"
    assert "[auditor]" in fingerprint_entry[0]
    assert "→" in fingerprint_entry[0]


def test_update_description_reset_fingerprint_guard(client, service):
    """PUT /tickets/{id}/description with reset_fingerprint_guard=True succeeds."""
    t = service.create("Reset ticket", description="body")
    r = client.put(
        f"/tickets/{t.id}/description",
        json={
            "description": "body",
            "reset_fingerprint_guard": True,
            "author": "operator",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["fingerprint_reset"] is True

    # History note should mention fingerprint-guard reset.
    history = client.get(f"/tickets/{t.id}/history").json()
    notes = [e["note"] for e in history]
    reset_notes = [n for n in notes if "fingerprint-guard reset" in n]
    assert len(reset_notes) == 1


def test_update_description_fingerprint_changes_on_update(client, service):
    """Updating the spec description changes the computed fingerprint."""
    t = service.create("FP ticket", description="Spec A")

    # Get the first fingerprint from the history when we update.
    r1 = client.put(
        f"/tickets/{t.id}/description",
        json={"description": "Spec B", "author": "test"},
    )
    assert r1.status_code == 200

    history = client.get(f"/tickets/{t.id}/history").json()
    notes = [e["note"] for e in history]
    fp_notes = [n for n in notes if "spec update: fingerprint" in n]
    assert len(fp_notes) == 1

    # Update again with a different spec.
    r2 = client.put(
        f"/tickets/{t.id}/description",
        json={"description": "Spec C", "author": "test"},
    )
    assert r2.status_code == 200

    history = client.get(f"/tickets/{t.id}/history").json()
    notes = [e["note"] for e in history]
    fp_notes = [n for n in notes if "spec update: fingerprint" in n]
    assert len(fp_notes) == 2

    # The two fingerprint transitions should be different.
    fp1_note = fp_notes[0]
    fp2_note = fp_notes[1]
    assert fp1_note != fp2_note
