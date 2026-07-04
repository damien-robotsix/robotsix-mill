"""Tests for the test-gap agent and runner."""

import json
import threading

from pathlib import Path

from robotsix_mill.agents import test_gap as test_gap_agent
from robotsix_mill.runners.periodic_runner import run_test_gap_pass, TestGapPassResult
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


# --- Agent tests ---


def test_test_gap_system_prompt_covers_key_dimensions():
    """The test-gap agent prompt must cover key inspection dimensions."""
    p = test_gap_agent.SYSTEM_PROMPT.lower()
    for kw in (
        "zero dedicated",
        "indirect",
        "i/o surface",
        "state-transition",
        "memory",
    ):
        assert kw in p, f"test-gap prompt missing dimension cue: {kw}"
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p
    # Tool-priority guidance: prefer run_command for enumerable work.
    assert "run_command" in p, "prompt must mention run_command"
    assert any(
        phrase in p for phrase in ("line count", "enumerable", "file existence")
    ), "prompt must reference enumerable/deterministic file work for run_command"


def test_test_gap_prompt_is_language_agnostic():
    """The prompt must NOT hardcode the mill's own source root — it has to
    infer the source root / test pattern per repo so it can run on any
    registered repo (robotsix-llmio, robotsix-auto-mail, …), not just mill."""
    p = test_gap_agent.SYSTEM_PROMPT
    # No mill-specific package path baked in.
    assert "src/robotsix_mill" not in p
    pl = p.lower()
    # Must tell the agent to discover the layout itself.
    assert "language-agnostic" in pl
    assert "source root" in pl
    # References more than one ecosystem's build manifest.
    assert "pyproject.toml" in pl
    assert any(m in pl for m in ("cargo.toml", "go.mod", "package.json"))


def test_test_gap_result_model():
    """TestGapResult has the expected fields and defaults."""
    result = test_gap_agent.TestGapResult(
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
    default_result = test_gap_agent.TestGapResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_test_gap_result_field_types():
    """TestGapResult fields have correct types."""
    result = test_gap_agent.TestGapResult(
        updated_memory="# Test-Gap Memory\n",
        draft_titles=["Add unit tests for X"],
        draft_bodies=["Module X has no tests..."],
        gap_ids=["untested_module_x"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)


# --- Runner tests ---


def test_run_test_gap_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return test_gap_agent.TestGapResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_test_gap_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "test_gap_memory.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return test_gap_agent.TestGapResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == ["# Existing memory\n## Proposed\n- gap1\n"]


def test_run_test_gap_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- gap1\n"

    def mock_agent(**kwargs):
        return test_gap_agent.TestGapResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    memory_file = settings.data_dir / "test-repo" / "test_gap_memory.md"
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_test_gap_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap with
    source='test_gap'."""
    repo_config = _test_repo_config()
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id=repo_config.repo_id)
    service = TicketService(settings, board_id=repo_config.repo_id)

    def mock_agent(**kwargs):
        return test_gap_agent.TestGapResult(
            updated_memory="# Memory\n",
            draft_titles=[
                "test gap: add unit tests for agents/refining.py",
                "test gap: add unit tests for agents/coordinating.py",
            ],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["refining_untested", "coordinating_untested"],
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="test_gap"
    tickets = service.list()
    test_gap_tickets = [t for t in tickets if t.source == "test_gap"]
    assert len(test_gap_tickets) == 2
    assert test_gap_tickets[0].state == State.DRAFT


def test_run_test_gap_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return test_gap_agent.TestGapResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 0


def test_run_test_gap_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "test_gap_memory.md"
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return test_gap_agent.TestGapResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_test_gap_pass_skips_empty_title_or_body(tmp_path, monkeypatch):
    """Runner skips draft entries with empty title or body."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return test_gap_agent.TestGapResult(
            updated_memory="mem",
            draft_titles=["Valid", "", "Also Valid"],
            draft_bodies=["Body", "Body2", ""],
            gap_ids=["g1", "g2", "g3"],
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 1  # only first has both title + body


def test_test_gap_pass_result_structure(tmp_path, monkeypatch):
    """TestGapPassResult has correct structure."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return test_gap_agent.TestGapResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert isinstance(result, TestGapPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


# --- Config tests ---


def test_test_gap_config_defaults():
    """Test-gap config has correct defaults."""
    s = Settings()
    assert s.test_gap_periodic is True
    assert s.test_gap_interval_seconds == 86400


def test_test_gap_periodic_config():
    """Test-gap periodic can be enabled."""
    s = Settings(test_gap_periodic="true", test_gap_interval_seconds="43200")
    assert s.test_gap_periodic is True
    assert s.test_gap_interval_seconds == 43200


# --- CLI tests ---


def test_test_gap_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI test-gap command works."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return TestGapPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "test gap: add unit tests for X"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_test_gap_pass", mock_run
    )

    result = main(["test-gap"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Test-gap pass complete" in captured.out
    assert "test gap: add unit tests for X" in captured.out


def test_test_gap_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag for test-gap CLI."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return TestGapPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "test gap: add unit tests for X"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_test_gap_pass", mock_run
    )

    result = main(["test-gap", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data
    assert data["tickets_created"] == [
        {"id": "123", "title": "test gap: add unit tests for X"}
    ]


def test_test_gap_cli_no_drafts(capsys, tmp_path, monkeypatch):
    """CLI test-gap command when no drafts created."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return TestGapPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_test_gap_pass", mock_run
    )

    result = main(["test-gap"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No new draft tickets created" in captured.out


def test_test_gap_cli_failure(capsys, monkeypatch):
    """CLI test-gap exits 1 on failure."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_test_gap_pass", mock_run
    )

    result = main(["test-gap"])
    assert result == 1
    captured = capsys.readouterr()
    assert "test-gap failed" in captured.err


# --- Langfuse session tests ---


def test_run_test_gap_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """session_id is passed through to the result — tracing is now the
    poll loop's responsibility."""

    settings = _make_settings(tmp_path)
    seen = {}

    def mock_agent(**kwargs):
        seen["agent_ran"] = True
        return test_gap_agent.TestGapResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    res = run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())

    assert res.session_id == "test-sid"
    assert seen["agent_ran"] is True


def test_test_gap_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        test_gap_agent,
        "run_test_gap_agent",
        lambda **k: test_gap_agent.TestGapResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    a = run_test_gap_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    ).session_id
    assert a == "test-sid"


# --- Clone tests ---


def test_run_test_gap_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the test-gap run clones the repo locally
    and hands the agent repo_dir."""
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
        return test_gap_agent.TestGapResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(test_gap_agent, "run_test_gap_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    repo = settings.data_dir / "test_gap_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo

    seen["clone"] = 0
    run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert seen["clone"] == 1 and seen["repo_dir"] == repo  # re-clones fresh each run


def test_run_test_gap_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)  # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        test_gap_agent,
        "run_test_gap_agent",
        lambda **k: (
            got.__setitem__("repo_dir", k.get("repo_dir"))
            or test_gap_agent.TestGapResult(
                updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
            )
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    run_test_gap_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert got["repo_dir"] is None


# --- Worker periodic tests ---


# --- API endpoint tests ---


def test_post_test_gap_returns_202(tmp_path, monkeypatch, repos_registry):
    """POST /test-gap returns 202 immediately, runs in background."""
    from fastapi.testclient import TestClient

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    started = threading.Event()
    finished = threading.Event()

    def slow_run(session_id=None, repo_config=None):
        started.set()
        finished.wait(timeout=5)
        return TestGapPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_test_gap_pass", slow_run
    )

    from robotsix_mill.runtime.api import create_app

    app = create_app(repos_registry, settings, single_repo_id="test-repo")
    with TestClient(app) as client:
        response = client.post("/test-gap")
        assert response.status_code == 202
        assert response.json() == {"status": "started"}

        # Background thread should have started
        assert started.wait(timeout=3), "Background thread did not start"

        # Clean up
        finished.set()


def test_post_test_gap_runs_in_background(tmp_path, monkeypatch, repos_registry):
    """POST /test-gap runs in background thread, drafts appear."""
    from fastapi.testclient import TestClient

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    run_event = threading.Event()

    def mock_run(session_id=None, repo_config=None):
        # Create a ticket directly in DB to simulate the runner
        svc = TicketService(settings, board_id="test-board")
        svc.create("Test-gap draft", "Test-gap body", source="test_gap")
        run_event.set()
        return TestGapPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "test-id", "title": "Test-gap draft"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_test_gap_pass", mock_run
    )

    from robotsix_mill.runtime.api import create_app

    app = create_app(repos_registry, settings, single_repo_id="test-repo")
    with TestClient(app) as client:
        response = client.post("/test-gap")
        assert response.status_code == 202

        # Wait for background thread to complete
        assert run_event.wait(timeout=5), "Background thread did not complete"

        # Verify the draft ticket was created
        svc = TicketService(settings, board_id="test-board")
        tickets = svc.list()
        test_gap_tickets = [t for t in tickets if t.source == "test_gap"]
        assert len(test_gap_tickets) == 1
        assert test_gap_tickets[0].title == "Test-gap draft"


# --- Board HTML tests ---


def test_board_html_contains_test_gap_button():
    """Board HTML contains the 'Test Gaps' button; the JS file
    references the /test-gap endpoint."""
    from robotsix_mill.runtime.board_html import BOARD_HTML

    assert "Test Gaps" in BOARD_HTML
    assert "runTestGap()" in BOARD_HTML

    import robotsix_mill.runtime.board_html

    js = (
        Path(robotsix_mill.runtime.board_html.__file__).parent
        / "static"
        / "board-mill.js"
    ).read_text()
    assert "/test-gap" in js


def test_board_contains_test_gap_js_and_css():
    """The static CSS file contains .src-test_gap class; the JS file
    maps the 'test_gap' source in srcClass()."""
    import robotsix_mill.runtime.board_html

    base = Path(robotsix_mill.runtime.board_html.__file__).parent / "static"
    css = (base / "board-mill.css").read_text()
    js = (base / "board-mill.js").read_text()
    assert ".src-test-gap" in css
    assert "src-test-gap" in css
    assert "test_gap" in js
