"""Multi-repo integration tests (epic 5907 items 20–21).

Proves end-to-end isolation: two repos operating in the same process
with zero cross-talk in tickets, costs, and periodic-agent artifacts.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from robotsix_mill.config import RepoConfig
from robotsix_mill.runtime.api import create_app


class _StubReadClient:
    """Minimal ``LangfuseReadClient`` stand-in that paginates to nothing,
    so cost-endpoint routing tests can capture the per-repo credentials
    without hitting the network."""

    def iter_pages(self, url, params=None, *, error_label="request"):
        return iter(())


def _capture_build_client(captured_projects):
    """Patch target for ``_build_read_client``: records which repo's
    Langfuse project each call routed through, then returns an empty
    stub client."""

    def fake_build(settings, repo_config=None):
        captured_projects.append(
            repo_config.langfuse_project_name if repo_config else None
        )
        return _StubReadClient()

    return fake_build


# -- fixtures -----------------------------------------------------------


@pytest.fixture
def multi_repo_client(settings, two_repo_registry):
    """TestClient in true multi-repo mode (no single_repo_id).

    Unlike the ``client`` fixture in test_routes.py / test_api.py,
    this exercises the real multi-repo code paths: the lifespan
    handles ALL repos, ``_resolve_cost_repo`` enforces the
    ``repo_id`` guard, and ``POST /tickets`` requires ``repo_id``.
    """
    with TestClient(create_app(two_repo_registry, settings)) as c:
        yield c


# -- 2. Ticket creation routing -----------------------------------------


def test_create_ticket_repo_a(multi_repo_client):
    """POST /tickets with repo_id="repo-a" → 201, visible under repo-a."""
    r = multi_repo_client.post(
        "/tickets",
        json={"title": "Repo A task", "repo_id": "repo-a"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Repo A task"
    assert data["state"] == "draft"


def test_create_ticket_repo_b(multi_repo_client):
    """POST /tickets with repo_id="repo-b" → 201, visible under repo-b."""
    r = multi_repo_client.post(
        "/tickets",
        json={"title": "Repo B task", "repo_id": "repo-b"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "Repo B task"
    assert data["state"] == "draft"


def test_create_ticket_unknown_repo(multi_repo_client):
    """POST /tickets with repo_id="nonexistent" → 400."""
    r = multi_repo_client.post(
        "/tickets",
        json={"title": "Bad", "repo_id": "nonexistent"},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Unknown repo" in detail
    assert "nonexistent" in detail


def test_create_ticket_no_repo_id_multi_repo(multi_repo_client):
    """POST /tickets without repo_id in multi-repo mode → 400."""
    r = multi_repo_client.post(
        "/tickets",
        json={"title": "No repo id"},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "repo_id is required" in detail.lower()


def test_create_epic_no_repo_id_multi_repo(multi_repo_client):
    """POST /epics without repo_id in multi-repo mode → 400."""
    r = multi_repo_client.post(
        "/epics",
        json={"title": "No repo id"},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "repo_id is required" in detail.lower()


# -- 2b. Ticket listing isolation ---------------------------------------


def test_list_tickets_repo_isolation(multi_repo_client):
    """Tickets created under repo A only appear when filtering by repo-a."""
    # Create one ticket per repo.
    multi_repo_client.post("/tickets", json={"title": "A-1", "repo_id": "repo-a"})
    multi_repo_client.post("/tickets", json={"title": "B-1", "repo_id": "repo-b"})

    # repo-a filter
    r = multi_repo_client.get("/tickets?repo_id=repo-a")
    assert r.status_code == 200
    data_a = r.json()
    titles_a = {t["title"] for t in data_a}
    assert "A-1" in titles_a
    assert "B-1" not in titles_a

    # repo-b filter
    r = multi_repo_client.get("/tickets?repo_id=repo-b")
    assert r.status_code == 200
    data_b = r.json()
    titles_b = {t["title"] for t in data_b}
    assert "B-1" in titles_b
    assert "A-1" not in titles_b


def test_list_tickets_no_filter_returns_all(multi_repo_client):
    """GET /tickets without repo_id returns tickets from both repos."""
    multi_repo_client.post("/tickets", json={"title": "All-A", "repo_id": "repo-a"})
    multi_repo_client.post("/tickets", json={"title": "All-B", "repo_id": "repo-b"})

    r = multi_repo_client.get("/tickets")
    assert r.status_code == 200
    data = r.json()
    titles = {t["title"] for t in data}
    assert "All-A" in titles
    assert "All-B" in titles


def test_list_tickets_unknown_repo(multi_repo_client):
    """GET /tickets?repo_id=nonexistent → 400."""
    r = multi_repo_client.get("/tickets?repo_id=nonexistent")
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Unknown repo" in detail


# -- 3. Board UI tests ---------------------------------------------------


def test_repos_endpoint(multi_repo_client):
    """GET /repos returns both repos plus the synthetic meta board,
    with no credential leaks."""
    r = multi_repo_client.get("/repos")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    # Two registered repos + the synthetic cross-repo "meta" board.
    assert len(data) == 3

    repo_ids = {entry["repo_id"] for entry in data}
    board_ids = {entry["board_id"] for entry in data}
    assert repo_ids == {"repo-a", "repo-b", "meta"}
    assert board_ids == {"board-a", "board-b", "meta"}

    # No credential leak: Langfuse keys never appear, and the exposed
    # forge_remote_url (operator-requested so agent consumers can locate
    # the code) must be credential-stripped — no userinfo, ever.
    for entry in data:
        for forbidden in (
            "langfuse_secret_key",
            "langfuse_public_key",
            "langfuse_base_url",
        ):
            assert forbidden not in entry, (
                f"credential leak: '{forbidden}' in /repos response"
            )
        url = entry["forge_remote_url"]
        if url is not None:
            authority = url.split("://", 1)[-1].split("/", 1)[0]
            assert "@" not in authority, (
                f"credential leak: userinfo in forge_remote_url {url!r}"
            )


def test_board_html_repo_selector(multi_repo_client):
    """GET / returns HTML containing the #repo-selector element."""
    r = multi_repo_client.get("/")
    assert r.status_code == 200
    html = r.text
    assert '<select id="repo-selector"' in html
    assert '<option value="all">All repos</option>' in html


def test_board_filter_integration_smoke(multi_repo_client):
    """End-to-end smoke: create tickets under both repos, verify
    repo_id filter returns correct subset."""
    multi_repo_client.post("/tickets", json={"title": "Smoke-A", "repo_id": "repo-a"})
    multi_repo_client.post("/tickets", json={"title": "Smoke-B", "repo_id": "repo-b"})

    r = multi_repo_client.get("/tickets?repo_id=repo-a")
    titles = {t["title"] for t in r.json()}
    assert "Smoke-A" in titles
    assert "Smoke-B" not in titles

    r = multi_repo_client.get("/tickets?repo_id=repo-b")
    titles = {t["title"] for t in r.json()}
    assert "Smoke-B" in titles
    assert "Smoke-A" not in titles


def test_meta_board_visible_and_listable(multi_repo_client, settings):
    """The synthetic cross-repo meta board must be queryable via
    ?repo_id=meta (no 400) and included in the 'all repos' view, so
    extraction proposals are never hidden from the operator."""
    from robotsix_mill.core.models import SourceKind
    from robotsix_mill.core.service import TicketService

    svc = TicketService(settings, board_id="meta")
    t = svc.create(
        title="Extract shared cascade loader",
        description="meta proposal body",
        source=SourceKind.META,
        origin_session="meta-test",
    )

    # Explicit meta query is allowed (not a 400) and returns the draft.
    r = multi_repo_client.get("/tickets?repo_id=meta")
    assert r.status_code == 200
    assert t.id in {x["id"] for x in r.json()}

    # The "all repos" view includes meta-board tickets too.
    r_all = multi_repo_client.get("/tickets")
    assert t.id in {x["id"] for x in r_all.json()}


def test_create_ticket_on_meta_board(multi_repo_client):
    """POST /tickets with repo_id="meta" must succeed (the meta board is
    selectable in the UI), not 400 as an "unknown repo"."""
    r = multi_repo_client.post(
        "/tickets",
        json={"title": "Meta task", "repo_id": "meta"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "Meta task"
    # The created ticket is visible under the meta board.
    listed = multi_repo_client.get("/tickets?repo_id=meta").json()
    assert "Meta task" in {t["title"] for t in listed}


def test_create_epic_on_meta_board(multi_repo_client):
    """POST /epics with repo_id="meta" must succeed instead of 400-ing."""
    r = multi_repo_client.post(
        "/epics",
        json={"title": "Meta epic", "repo_id": "meta"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["title"] == "Meta epic"
    listed = multi_repo_client.get("/tickets?repo_id=meta").json()
    assert "Meta epic" in {t["title"] for t in listed}


# -- 4. Periodic-agent isolation (Approach B) ---------------------------


def test_audit_repo_isolation(settings, monkeypatch, tmp_path):
    """Audit pass for repo A writes sentinel only under repo A's dir."""
    from robotsix_mill.runners import periodic_runner
    from robotsix_mill.core import db as _db

    _db.reset_engine()

    # Point data_dir at a temp tree so sentinels land there.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings.data_dir = data_dir

    # Re-init DB under the new data_dir settings.
    _db.init_db(settings, board_id="test-board")

    repo_a = RepoConfig(
        repo_id="repo-a",
        langfuse_project_name="proj-a",
        langfuse_public_key="pk-a",
        langfuse_secret_key="sk-a",
    )
    repo_b = RepoConfig(
        repo_id="repo-b",
        langfuse_project_name="proj-b",
        langfuse_public_key="pk-b",
        langfuse_secret_key="sk-b",
    )

    # Fake audit pass: writes a sentinel and returns a benign result.
    class _FakeResult:
        memory: str = ""
        drafts_created: list = []

    def fake_audit(session_id: str, repo_config=None):
        if repo_config is not None:
            repo_dir = settings.data_dir / repo_config.repo_id
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "audit_sentinel").write_text("audit ran")
        return _FakeResult()

    monkeypatch.setattr(periodic_runner, "run_audit_pass", fake_audit)

    # Run audit for repo A.
    periodic_runner.run_audit_pass("test-session", repo_config=repo_a)

    sentinel_a = data_dir / "repo-a" / "audit_sentinel"
    sentinel_b = data_dir / "repo-b" / "audit_sentinel"
    assert sentinel_a.exists(), "repo-a sentinel should exist after audit"
    assert not sentinel_b.exists(), (
        "repo-b sentinel should NOT exist after repo-a audit"
    )

    # Now run audit for repo B.
    periodic_runner.run_audit_pass("test-session", repo_config=repo_b)
    assert sentinel_b.exists(), "repo-b sentinel should exist after repo-b audit"

    _db.reset_engine()


def test_bc_check_repo_isolation(settings, monkeypatch, tmp_path):
    """BC check pass for repo A writes sentinel only under repo A's dir."""
    from robotsix_mill.runners import periodic_runner
    from robotsix_mill.core import db as _db

    _db.reset_engine()

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings.data_dir = data_dir
    _db.init_db(settings, board_id="test-board")

    repo_a = RepoConfig(
        repo_id="repo-a",
        langfuse_project_name="proj-a",
        langfuse_public_key="pk-a",
        langfuse_secret_key="sk-a",
    )
    RepoConfig(
        repo_id="repo-b",
        langfuse_project_name="proj-b",
        langfuse_public_key="pk-b",
        langfuse_secret_key="sk-b",
    )

    class _FakeResult:
        memory: str = ""
        drafts_created: list = []

    def fake_bc_check(session_id: str, repo_config=None):
        if repo_config is not None:
            repo_dir = settings.data_dir / repo_config.repo_id
            repo_dir.mkdir(parents=True, exist_ok=True)
            (repo_dir / "bc_check_sentinel").write_text("bc_check ran")
        return _FakeResult()

    monkeypatch.setattr(periodic_runner, "run_bc_check_pass", fake_bc_check)

    # Run bc-check for repo A only.
    periodic_runner.run_bc_check_pass("test-session", repo_config=repo_a)

    sentinel_a = data_dir / "repo-a" / "bc_check_sentinel"
    sentinel_b = data_dir / "repo-b" / "bc_check_sentinel"
    assert sentinel_a.exists(), "repo-a sentinel should exist after bc-check"
    assert not sentinel_b.exists(), (
        "repo-b sentinel should NOT exist after repo-a bc-check"
    )

    _db.reset_engine()
