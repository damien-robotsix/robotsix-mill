"""Tests for the config-sync runner."""

from robotsix_mill.runners.periodic_runner import (
    run_config_sync_pass,
    PeriodicPassResult,
)
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


# --- Runner tests ---


def test_run_config_sync_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    from robotsix_mill.agents import config_syncing

    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return config_syncing.ConfigSyncResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(config_syncing, "run_config_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_config_sync_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_config_sync_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    from robotsix_mill.agents import config_syncing

    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "config_sync_memory.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return config_syncing.ConfigSyncResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(config_syncing, "run_config_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_config_sync_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == ["# Existing memory\n## Proposed\n- gap1\n"]


def test_run_config_sync_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    from robotsix_mill.agents import config_syncing

    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- gap1\n"

    def mock_agent(**kwargs):
        return config_syncing.ConfigSyncResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(config_syncing, "run_config_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_config_sync_pass(session_id="test-sid", repo_config=_test_repo_config())
    memory_file = settings.data_dir / "test-repo" / "config_sync_memory.md"
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_config_sync_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap with
    source='config_sync'."""
    from robotsix_mill.agents import config_syncing

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return config_syncing.ConfigSyncResult(
            updated_memory="# Memory\n",
            draft_titles=[
                "config drift: FOO missing from .env",
                "config drift: BAR missing from docs/config/configuration.md",
            ],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["foo_missing_env", "bar_missing_docs"],
        )

    monkeypatch.setattr(config_syncing, "run_config_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_config_sync_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="config_sync"
    tickets = service.list()
    config_sync_tickets = [t for t in tickets if t.source == "config_sync"]
    assert len(config_sync_tickets) == 2
    assert config_sync_tickets[0].state == State.DRAFT


def test_run_config_sync_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    from robotsix_mill.agents import config_syncing

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return config_syncing.ConfigSyncResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(config_syncing, "run_config_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_config_sync_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert len(result.drafts_created) == 0


def test_run_config_sync_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    from robotsix_mill.agents import config_syncing

    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "config_sync_memory.md"
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return config_syncing.ConfigSyncResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(config_syncing, "run_config_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_config_sync_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_config_sync_pass_result_structure(tmp_path, monkeypatch):
    """PeriodicPassResult has correct structure."""
    from robotsix_mill.agents import config_syncing

    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return config_syncing.ConfigSyncResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    monkeypatch.setattr(config_syncing, "run_config_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_config_sync_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert isinstance(result, PeriodicPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


# --- Session id pass-through tests ---


def test_run_config_sync_pass_session_id_passed_through(tmp_path, monkeypatch):
    """session_id is stored in the result — the agent runs under it via
    origin_session on the ticket."""
    from robotsix_mill.agents import config_syncing

    settings = _make_settings(tmp_path)
    seen = {}

    def mock_agent(**kwargs):
        seen["agent_ran"] = True
        return config_syncing.ConfigSyncResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(config_syncing, "run_config_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    res = run_config_sync_pass(session_id="test-sid", repo_config=_test_repo_config())

    assert res.session_id == "test-sid"
    assert seen["agent_ran"] is True
