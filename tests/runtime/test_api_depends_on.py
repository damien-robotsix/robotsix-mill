import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.models import TicketKind
from robotsix_mill.core.states import State
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
    assert done.id in ids_default
    assert draft.id in ids_default


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
