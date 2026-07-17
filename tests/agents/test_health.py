"""Tests for the health agent and runner."""

import json
import threading

import pytest
from pathlib import Path

from robotsix_mill.agents import health as health_agent
from robotsix_mill.runners.periodic_runner import run_health_pass, PeriodicPassResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def _test_repo_config():
    """Synthetic RepoConfig for periodic-runner tests — the runner now
    requires one (mono-repo board-less mode is gone)."""
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test-project",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
    )


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    return s


class _FakeHealthAgentResult:
    """Returned by mock health agent callables — matches the interface
    that run_agent_pass accesses."""

    def __init__(self, updated_memory, draft_titles, draft_bodies, gap_ids=None):
        self.updated_memory = updated_memory
        self.draft_titles = draft_titles
        self.draft_bodies = draft_bodies
        if gap_ids is not None:
            self.gap_ids = gap_ids


def _make_health_agent(
    updated_memory="new memory", draft_titles=None, draft_bodies=None, gap_ids=None
):
    """Return a callable that returns a _FakeHealthAgentResult with the given data."""
    if draft_titles is None:
        draft_titles = []
    if draft_bodies is None:
        draft_bodies = []

    def agent_fn(*, settings, memory, recent_proposals="", verified_proposals=""):
        return _FakeHealthAgentResult(
            updated_memory=updated_memory,
            draft_titles=draft_titles,
            draft_bodies=draft_bodies,
            gap_ids=gap_ids,
        )

    return agent_fn


# --- Agent tests ---


def test_health_system_prompt_covers_all_six_dimensions():
    """The health agent prompt must cover all six inspection dimensions:
    module size, function length, documentation coverage, test gaps,
    complexity, and dead code."""
    p = health_agent.SYSTEM_PROMPT.lower()
    for kw in (
        "module size",
        "function length",
        "documentation coverage",
        "test gaps",
        "complexity",
        "dead code",
    ):
        assert kw in p, f"health prompt missing dimension cue: {kw}"
    # Must exercise judgement, not just thresholds.
    assert "judgement" in p or "judgment" in p
    assert "not a static linter" in p or "static linter" in p
    # Must use the memory ledger to avoid re-nagging.
    assert "memory" in p
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p


def test_health_result_model():
    """HealthResult has the expected fields and defaults."""
    result = health_agent.HealthResult(
        updated_memory="memory",
        draft_titles=["title1"],
        draft_bodies=["body1"],
        gap_ids=["gap1"],
    )
    assert result.updated_memory == "memory"
    assert len(result.draft_titles) == 1
    assert len(result.draft_bodies) == 1
    assert len(result.gap_ids) == 1

    # Defaults
    default_result = health_agent.HealthResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_health_result_field_types():
    """HealthResult fields have correct types."""
    result = health_agent.HealthResult(
        updated_memory="# Health Memory\n",
        draft_titles=["Fix module X"],
        draft_bodies=["Module X is too large..."],
        gap_ids=["oversized_module_x"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)


# --- Runner tests ---


def test_run_health_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return health_agent.HealthResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_health_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "health_memory.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return health_agent.HealthResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == ["# Existing memory\n## Proposed\n- gap1\n"]


def test_run_health_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- gap1\n"

    def mock_agent(**kwargs):
        return health_agent.HealthResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    memory_file = settings.data_dir / "test-repo" / "health_memory.md"
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_health_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap with
    source='health'."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return health_agent.HealthResult(
            updated_memory="# Memory\n",
            draft_titles=["Fix gap1", "Fix gap2"],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["gap1", "gap2"],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="health"
    tickets = service.list()
    health_tickets = [t for t in tickets if t.source == "health"]
    assert len(health_tickets) == 2
    assert health_tickets[0].state == State.DRAFT


def test_run_health_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return health_agent.HealthResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 0


def test_run_health_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "health_memory.md"
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return health_agent.HealthResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_health_pass_unreadable_memory(tmp_path, monkeypatch):
    """Unreadable memory file -> empty string, no error."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        kwargs.get("memory", "")
        return health_agent.HealthResult(
            updated_memory="mem",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)

    # Have the periodic_runner pick up our tmp-scoped settings, then
    # make memory_file_for("health", …) return a path whose
    # read_text() raises OSError so load_memory's OSError-handling
    # branch is exercised.
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    from unittest.mock import MagicMock

    unreadable = MagicMock()
    unreadable.exists.return_value = True
    unreadable.read_text.side_effect = OSError("permission denied")
    monkeypatch.setattr(
        type(settings),
        "memory_file_for",
        lambda self, name, board_id="": unreadable,
    )

    result = run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    # Should not raise; agent gets empty memory
    assert result.updated_memory == "mem"


def test_health_pass_result_structure(tmp_path, monkeypatch):
    """PeriodicPassResult has correct structure."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return health_agent.HealthResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert isinstance(result, PeriodicPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


def test_run_health_pass_skips_empty_title_or_body(tmp_path, monkeypatch):
    """Runner skips draft entries with empty title or body."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return health_agent.HealthResult(
            updated_memory="mem",
            draft_titles=["Valid", "", "Also Valid"],
            draft_bodies=["Body", "Body2", ""],
            gap_ids=["g1", "g2", "g3"],
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 1  # only first has both title + body


# --- Config tests ---


def test_health_config_defaults():
    """Health config has correct defaults."""
    s = Settings()
    assert s.health_periodic is True
    assert s.health_interval_seconds == 604800


def test_health_periodic_config():
    """Health periodic can be enabled."""
    s = Settings(health_periodic="true", health_interval_seconds="43200")
    assert s.health_periodic is True
    assert s.health_interval_seconds == 43200


# --- CLI tests ---


def test_health_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI health command works."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_health_pass", mock_run
    )

    result = main(["health"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Health pass complete" in captured.out
    assert "Fix gap" in captured.out


def test_health_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag for health CLI."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_health_pass", mock_run
    )

    result = main(["health", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data
    assert data["tickets_created"] == [{"id": "123", "title": "Fix gap"}]


def test_health_cli_no_drafts(capsys, tmp_path, monkeypatch):
    """CLI health command when no drafts created."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_health_pass", mock_run
    )

    result = main(["health"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No new draft tickets created" in captured.out


def test_health_cli_failure(capsys, monkeypatch):
    """CLI health exits 1 on failure."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_health_pass", mock_run
    )

    result = main(["health"])
    assert result == 1
    captured = capsys.readouterr()
    assert "health failed" in captured.err


# --- Langfuse session tests ---


def test_run_health_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """session_id is passed through to the result — tracing is now the
    poll loop's responsibility."""
    from robotsix_mill.agents import health as health_agent

    settings = _make_settings(tmp_path)
    seen = {}

    def mock_agent(**kwargs):
        seen["agent_ran"] = True
        return health_agent.HealthResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    res = run_health_pass(session_id="test-sid", repo_config=_test_repo_config())

    assert res.session_id == "test-sid"
    assert seen["agent_ran"] is True


def test_health_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        health_agent,
        "run_health_agent",
        lambda **k: health_agent.HealthResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    a = run_health_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    ).session_id
    assert a == "test-sid"


# --- Clone tests ---


def test_run_health_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the health run clones the repo locally
    and hands the agent repo_dir. Idempotent + best-effort."""
    from robotsix_mill.vcs import git_ops

    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )
    seen = {"clone": 0, "repo_dir": "unset"}

    def fake_clone(url, dest, branch, token):
        seen["clone"] += 1
        (dest / ".git").mkdir(parents=True)

    def mock_agent(**kwargs):
        seen["repo_dir"] = kwargs.get("repo_dir")
        return health_agent.HealthResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(health_agent, "run_health_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    repo = settings.data_dir / "health_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo

    seen["clone"] = 0
    run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert seen["clone"] == 1 and seen["repo_dir"] == repo  # re-clones fresh each run


def test_run_health_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)  # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        health_agent,
        "run_health_agent",
        lambda **k: (
            got.__setitem__("repo_dir", k.get("repo_dir"))
            or health_agent.HealthResult(
                updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
            )
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    run_health_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert got["repo_dir"] is None


# --- Worker periodic tests ---


@pytest.mark.asyncio
async def test_worker_spawns_periodic_supervisor_per_repo(
    tmp_path, monkeypatch, repo_config
):
    """Periodic agents (incl. health) no longer get a static per-agent task;
    they run via the per-repo periodic supervisor, which start() spawns once
    per registered repo. The supervisor reads .robotsix-mill/periodic/ presence
    files from each repo's clone to decide what runs."""
    import asyncio as asyncio_mod

    from robotsix_mill.config import ReposRegistry
    from robotsix_mill.runtime.worker import Worker
    from robotsix_mill.stages import StageContext

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")
    ctx = StageContext(settings=settings, service=service, repo_config=repo_config)

    # Park the supervisor so it doesn't clone/network during the test.
    async def noop_supervisor(self, rc):
        await asyncio_mod.sleep(3600)

    monkeypatch.setattr(Worker, "_periodic_supervisor", noop_supervisor)
    monkeypatch.setattr(
        "robotsix_mill.runtime.worker.core.get_repos_config",
        lambda: ReposRegistry(repos={repo_config.repo_id: repo_config}),
    )

    worker = Worker(ctx)
    worker.start()

    assert repo_config.board_id in worker._periodic_supervisor_tasks
    assert not worker._periodic_supervisor_tasks[repo_config.board_id].done()
    # No static per-agent health task exists anymore.
    assert getattr(worker, "_health_task", None) is None

    await worker.stop()


# --- API endpoint tests ---


def test_post_health_check_returns_202(tmp_path, monkeypatch, repos_registry):
    """POST /health-check returns 202 immediately, runs in background."""
    from fastapi.testclient import TestClient

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    started = threading.Event()
    finished = threading.Event()

    def slow_run(session_id=None, repo_config=None):
        started.set()
        finished.wait(timeout=5)
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_health_pass", slow_run
    )

    from robotsix_mill.runtime.api import create_app

    app = create_app(repos_registry, settings, single_repo_id="test-repo")
    # `with TestClient(app)` triggers FastAPI lifespan startup, which
    # populates app.state.run_registry — required now that /health-check
    # registers its in-flight run there for the board's Runs panel.
    with TestClient(app) as client:
        response = client.post("/health-check")
        assert response.status_code == 202
        assert response.json() == {"status": "started"}

        # Background thread should have started
        assert started.wait(timeout=3), "Background thread did not start"

        # Clean up
        finished.set()


def test_post_health_check_runs_in_background(tmp_path, monkeypatch, repos_registry):
    """POST /health-check runs in background thread, drafts appear."""
    from fastapi.testclient import TestClient

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    run_event = threading.Event()

    def mock_run(session_id=None, repo_config=None):
        # Create a ticket directly in DB to simulate the runner
        svc = TicketService(settings, board_id="test-board")
        svc.create("Health draft", "Health body", source="health")
        run_event.set()
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "test-id", "title": "Health draft"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_health_pass", mock_run
    )

    from robotsix_mill.runtime.api import create_app

    app = create_app(repos_registry, settings, single_repo_id="test-repo")
    # Lifespan-aware client so app.state.run_registry is initialised
    # (the /health-check route now records the in-flight run there).
    with TestClient(app) as client:
        response = client.post("/health-check")
        assert response.status_code == 202

        # Wait for background thread to complete
        assert run_event.wait(timeout=5), "Background thread did not complete"

        # Verify the draft ticket was created
        svc = TicketService(settings, board_id="test-board")
        tickets = svc.list()
        health_tickets = [t for t in tickets if t.source == "health"]
        assert len(health_tickets) == 1
        assert health_tickets[0].title == "Health draft"


# --- Live-filesystem guard ---


def test_health_draft_blocked_when_test_dir_exists(tmp_path, monkeypatch):
    """A health-agent draft claiming a missing test directory is
    blocked when the directory already contains test_*.py files."""
    from robotsix_mill.runners.pass_runner import run_agent_pass
    from robotsix_mill.core.models import SourceKind

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    # Create the test directory with a test file already in it.
    test_dir = tmp_path / "tests" / "vcs"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "test_git_ops.py").write_text("# tests exist\n", encoding="utf-8")

    memory_file = tmp_path / "health_memory.md"
    memory_file.write_text("mem", encoding="utf-8")

    agent_fn = _make_health_agent(
        updated_memory="mem",
        draft_titles=["Add tests/vcs/ test subdirectory for git_ops.py"],
        draft_bodies=["Body for vcs test subdirectory"],
    )

    result = run_agent_pass(
        agent_fn,
        memory_file=memory_file,
        source_label=SourceKind.HEALTH,
        service=service,
        settings=settings,
        repo_dir=tmp_path,
    )

    # Draft should be skipped because the directory already has tests.
    assert result.drafts_created == []
    tickets = service.list()
    assert len(tickets) == 0

    db.reset_engine()


# --- Board HTML tests ---


def test_board_html_contains_health_button():
    """Board HTML contains the 'Health Check' button; the JS file
    references the /health-check endpoint."""
    from robotsix_mill.runtime.board_html import BOARD_HTML

    assert "Health Check" in BOARD_HTML
    assert "runHealth()" in BOARD_HTML

    import robotsix_mill.runtime.board_html

    js = (
        Path(robotsix_mill.runtime.board_html.__file__).parent
        / "static"
        / "board-mill.js"
    ).read_text()
    assert "/health-check" in js


def test_board_html_contains_health_css_class():
    """The static CSS file contains .src-health class; the JS file
    maps the 'health' source in srcClass()."""
    import robotsix_mill.runtime.board_html

    base = Path(robotsix_mill.runtime.board_html.__file__).parent / "static"
    css = (base / "board-mill.css").read_text()
    js = (base / "board-mill.js").read_text()
    assert ".src-health" in css
    assert "src-health" in css  # substring within .src-health rule
    assert '"health"' in js  # mapped in srcClass()
