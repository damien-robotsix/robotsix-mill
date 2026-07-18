"""Tests for the docstring-coverage agent and runner."""

import json
import threading

from pathlib import Path

from robotsix_mill.agents import docstring_coverage as dc_agent
from robotsix_mill.runners.periodic_runner import (
    run_docstring_coverage_pass,
    PeriodicPassResult,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def _test_repo_config():
    """Synthetic RepoConfig for periodic-runner tests."""
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


# --- Agent tests ---


def test_docstring_coverage_system_prompt_covers_key_dimensions():
    """The docstring-coverage agent prompt must cover key dimensions."""
    p = dc_agent.SYSTEM_PROMPT.lower()
    for kw in (
        "public",
        "docstring",
        "complexity",
        "delegation wrapper",
        "memory",
    ):
        assert kw in p, f"docstring-coverage prompt missing dimension cue: {kw}"
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p
    # Tool-priority guidance: prefer run_command for enumerable work.
    assert "run_command" in p, "prompt must mention run_command"


def test_docstring_coverage_prompt_covers_exclusions():
    """The prompt must mention exclusion categories."""
    p = dc_agent.SYSTEM_PROMPT.lower()
    for kw in ("trivial getter", "dunder", "property", "@override", "delegation"):
        assert kw in p, f"prompt missing exclusion cue: {kw}"


def test_docstring_coverage_prompt_covers_heuristics():
    """The prompt must mention heuristic thresholds."""
    p = dc_agent.SYSTEM_PROMPT.lower()
    assert ">10" in p or "> 10" in p, "prompt missing critical body-length threshold"
    assert ">5" in p or "> 5" in p, "prompt missing high body-length threshold"
    assert ">2" in p or "> 2" in p, "prompt missing parameter-count threshold"


def test_docstring_coverage_result_model():
    """DocstringCoverageResult has the expected fields and defaults."""
    result = dc_agent.DocstringCoverageResult(
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
    default_result = dc_agent.DocstringCoverageResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_docstring_coverage_result_field_types():
    """DocstringCoverageResult fields have correct types."""
    result = dc_agent.DocstringCoverageResult(
        updated_memory="# Docstring-Coverage Memory\n",
        draft_titles=["docstring gap: add docstring to foo in bar.py:42"],
        draft_bodies=["Function foo() in bar.py has no docstring..."],
        gap_ids=["missing_docstring_foo"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)


# --- Runner tests ---


def test_run_docstring_coverage_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return dc_agent.DocstringCoverageResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_docstring_coverage_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_docstring_coverage_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "docstring_coverage_memory.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return dc_agent.DocstringCoverageResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_docstring_coverage_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == ["# Existing memory\n## Proposed\n- gap1\n"]


def test_run_docstring_coverage_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- gap1\n"

    def mock_agent(**kwargs):
        return dc_agent.DocstringCoverageResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_docstring_coverage_pass(session_id="test-sid", repo_config=_test_repo_config())
    memory_file = settings.data_dir / "test-repo" / "docstring_coverage_memory.md"
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_docstring_coverage_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap with
    source='docstring_coverage'."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return dc_agent.DocstringCoverageResult(
            updated_memory="# Memory\n",
            draft_titles=[
                "docstring gap: add docstring to refine in agents/refining.py:42",
                "docstring gap: add docstring to implement in agents/coordinating.py:15",
            ],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["refine_nodoc", "implement_nodoc"],
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_docstring_coverage_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="docstring_coverage"
    tickets = service.list()
    dc_tickets = [t for t in tickets if t.source == "docstring_coverage"]
    assert len(dc_tickets) == 2
    assert dc_tickets[0].state == State.DRAFT


def test_run_docstring_coverage_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return dc_agent.DocstringCoverageResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_docstring_coverage_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert len(result.drafts_created) == 0


def test_run_docstring_coverage_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "docstring_coverage_memory.md"
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return dc_agent.DocstringCoverageResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_docstring_coverage_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_docstring_coverage_pass_skips_empty_title_or_body(tmp_path, monkeypatch):
    """Runner skips draft entries with empty title or body."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return dc_agent.DocstringCoverageResult(
            updated_memory="mem",
            draft_titles=["Valid", "", "Also Valid"],
            draft_bodies=["Body", "Body2", ""],
            gap_ids=["g1", "g2", "g3"],
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_docstring_coverage_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert len(result.drafts_created) == 1  # only first has both title + body


def test_docstring_coverage_pass_result_structure(tmp_path, monkeypatch):
    """PeriodicPassResult has correct structure."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return dc_agent.DocstringCoverageResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_docstring_coverage_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert isinstance(result, PeriodicPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


# --- Config tests ---


def test_docstring_coverage_config_defaults():
    """Docstring-coverage config has correct defaults."""
    s = Settings()
    assert s.docstring_coverage_periodic is True
    assert s.docstring_coverage_interval_seconds == 604800


def test_docstring_coverage_periodic_config():
    """Docstring-coverage periodic can be enabled."""
    s = Settings(
        docstring_coverage_periodic="true", docstring_coverage_interval_seconds="43200"
    )
    assert s.docstring_coverage_periodic is True
    assert s.docstring_coverage_interval_seconds == 43200


# --- CLI tests ---


def test_docstring_coverage_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI docstring-coverage command works."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None, repo_config=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[
                {
                    "id": "123",
                    "title": "docstring gap: add docstring to foo in bar.py:42",
                }
            ],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_docstring_coverage_pass", mock_run
    )

    result = main(["docstring-coverage"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Docstring-coverage pass complete" in captured.out
    assert "docstring gap: add docstring to foo in bar.py:42" in captured.out


def test_docstring_coverage_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag for docstring-coverage CLI."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None, repo_config=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[
                {
                    "id": "123",
                    "title": "docstring gap: add docstring to foo in bar.py:42",
                }
            ],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_docstring_coverage_pass", mock_run
    )

    result = main(["docstring-coverage", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data
    assert data["tickets_created"] == [
        {"id": "123", "title": "docstring gap: add docstring to foo in bar.py:42"}
    ]


def test_docstring_coverage_cli_no_drafts(capsys, tmp_path, monkeypatch):
    """CLI docstring-coverage command when no drafts created."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None, repo_config=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_docstring_coverage_pass", mock_run
    )

    result = main(["docstring-coverage"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No new draft tickets created" in captured.out


def test_docstring_coverage_cli_failure(capsys, monkeypatch):
    """CLI docstring-coverage exits 1 on failure."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None, repo_config=None):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_docstring_coverage_pass", mock_run
    )

    result = main(["docstring-coverage"])
    assert result == 1
    captured = capsys.readouterr()
    assert "docstring-coverage failed" in captured.err


# --- Langfuse session tests ---


def test_run_docstring_coverage_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """session_id is passed through to the result."""

    settings = _make_settings(tmp_path)
    seen = {}

    def mock_agent(**kwargs):
        seen["agent_ran"] = True
        return dc_agent.DocstringCoverageResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    res = run_docstring_coverage_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )

    assert res.session_id == "test-sid"
    assert seen["agent_ran"] is True


def test_docstring_coverage_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        dc_agent,
        "run_docstring_coverage_agent",
        lambda **k: dc_agent.DocstringCoverageResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    a = run_docstring_coverage_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    ).session_id
    assert a == "test-sid"


# --- Clone tests ---


def test_run_docstring_coverage_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the run clones the repo locally
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
        return dc_agent.DocstringCoverageResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(dc_agent, "run_docstring_coverage_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_docstring_coverage_pass(session_id="test-sid", repo_config=_test_repo_config())
    repo = settings.data_dir / "docstring_coverage_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo

    seen["clone"] = 0
    run_docstring_coverage_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert seen["clone"] == 1 and seen["repo_dir"] == repo  # re-clones fresh each run


def test_run_docstring_coverage_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)  # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        dc_agent,
        "run_docstring_coverage_agent",
        lambda **k: (
            got.__setitem__("repo_dir", k.get("repo_dir"))
            or dc_agent.DocstringCoverageResult(
                updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
            )
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    run_docstring_coverage_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert got["repo_dir"] is None


# --- API endpoint tests ---


def test_post_docstring_coverage_returns_202(tmp_path, monkeypatch, repos_registry):
    """POST /passes/docstring_coverage/run returns 202 immediately, runs in background."""
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
        "robotsix_mill.runners.periodic_runner.run_docstring_coverage_pass", slow_run
    )

    from robotsix_mill.runtime.api import create_app

    app = create_app(repos_registry, settings, single_repo_id="test-repo")
    with TestClient(app) as client:
        response = client.post("/passes/docstring_coverage/run")
        assert response.status_code == 202
        assert response.json() == {"status": "started"}

        # Background thread should have started
        assert started.wait(timeout=3), "Background thread did not start"

        # Clean up
        finished.set()


def test_post_docstring_coverage_runs_in_background(
    tmp_path, monkeypatch, repos_registry
):
    """POST /passes/docstring_coverage/run runs in background thread, drafts appear."""
    from fastapi.testclient import TestClient

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    run_event = threading.Event()

    def mock_run(session_id=None, repo_config=None):
        # Create a ticket directly in DB to simulate the runner
        svc = TicketService(settings, board_id="test-board")
        svc.create(
            "Docstring-coverage draft",
            "Docstring-coverage body",
            source="docstring_coverage",
        )
        run_event.set()
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "test-id", "title": "Docstring-coverage draft"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_docstring_coverage_pass", mock_run
    )

    from robotsix_mill.runtime.api import create_app

    app = create_app(repos_registry, settings, single_repo_id="test-repo")
    with TestClient(app) as client:
        response = client.post("/passes/docstring_coverage/run")
        assert response.status_code == 202

        # Wait for background thread to complete
        assert run_event.wait(timeout=5), "Background thread did not complete"

        # Verify the draft ticket was created
        svc = TicketService(settings, board_id="test-board")
        tickets = svc.list()
        dc_tickets = [t for t in tickets if t.source == "docstring_coverage"]
        assert len(dc_tickets) == 1
        assert dc_tickets[0].title == "Docstring-coverage draft"


# --- Board HTML tests ---


def test_board_html_contains_docstring_coverage_button():
    """The docstring-coverage pass is registered in the pass registry, so
    the dynamically-populated board dropdown exposes it as 'Doc Coverage'."""
    from robotsix_mill.runtime.routes._passes import _PASS_REGISTRY

    entry = _PASS_REGISTRY["docstring_coverage"]
    assert entry["label"] == "Doc Coverage"
    assert entry["runner_func"] == "run_docstring_coverage_pass"


def test_board_contains_docstring_coverage_js_and_css():
    """The static CSS file contains .src-docstring-coverage class; the JS file
    maps the 'docstring_coverage' source in srcClass()."""
    import robotsix_mill.runtime.board_html

    base = Path(robotsix_mill.runtime.board_html.__file__).parent / "static"
    css = (base / "board-mill.css").read_text()
    js = (base / "board-mill.js").read_text()
    assert ".src-docstring-coverage" in css
    assert "src-docstring-coverage" in css
    assert "docstring_coverage" in js
