import pytest
from fastapi.testclient import TestClient

from robotsix_mill.core.models import TicketKind
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
