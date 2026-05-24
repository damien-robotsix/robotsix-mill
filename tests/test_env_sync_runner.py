"""Tests for the env-sync runner."""

import pytest

from robotsix_mill.env_sync_runner import run_env_sync_pass, EnvSyncPassResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    return Settings(**overrides)


# --- Runner tests ---


def test_run_env_sync_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    from robotsix_mill.agents import env_syncing

    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return env_syncing.EnvSyncResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(env_syncing, "run_env_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )

    run_env_sync_pass()
    assert captured_memory == [""]


def test_run_env_sync_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    from robotsix_mill.agents import env_syncing

    settings = _make_settings(tmp_path)
    memory_file = settings.env_sync_memory_file
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return env_syncing.EnvSyncResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(env_syncing, "run_env_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )

    run_env_sync_pass()
    assert captured_memory == ["# Existing memory\n## Proposed\n- gap1\n"]


def test_run_env_sync_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    from robotsix_mill.agents import env_syncing

    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- gap1\n"

    def mock_agent(**kwargs):
        return env_syncing.EnvSyncResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(env_syncing, "run_env_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )

    run_env_sync_pass()
    memory_file = settings.env_sync_memory_file
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_env_sync_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap with
    source='env_sync'."""
    from robotsix_mill.agents import env_syncing

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)

    def mock_agent(**kwargs):
        return env_syncing.EnvSyncResult(
            updated_memory="# Memory\n",
            draft_titles=["env drift: FOO missing from .env",
                           "env drift: BAR missing from docs/configuration.md"],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["foo_missing_env", "bar_missing_docs"],
        )

    monkeypatch.setattr(env_syncing, "run_env_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )

    result = run_env_sync_pass()
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="env_sync"
    tickets = service.list()
    env_sync_tickets = [t for t in tickets if t.source == "env_sync"]
    assert len(env_sync_tickets) == 2
    assert env_sync_tickets[0].state == State.DRAFT


def test_run_env_sync_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    from robotsix_mill.agents import env_syncing

    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)

    def mock_agent(**kwargs):
        return env_syncing.EnvSyncResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(env_syncing, "run_env_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )

    result = run_env_sync_pass()
    assert len(result.drafts_created) == 0


def test_run_env_sync_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    from robotsix_mill.agents import env_syncing

    settings = _make_settings(tmp_path)
    memory_file = settings.env_sync_memory_file
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return env_syncing.EnvSyncResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(env_syncing, "run_env_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )

    result = run_env_sync_pass()
    assert captured_memory == [""]


def test_env_sync_pass_result_structure(tmp_path, monkeypatch):
    """EnvSyncPassResult has correct structure."""
    from robotsix_mill.agents import env_syncing

    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return env_syncing.EnvSyncResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    monkeypatch.setattr(env_syncing, "run_env_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )

    result = run_env_sync_pass()
    assert isinstance(result, EnvSyncPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


# --- Langfuse session tests ---


def test_run_env_sync_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """Each env-sync run wraps the agent in a Langfuse session span with a
    unique per-run id."""
    import contextlib

    from robotsix_mill.agents import env_syncing
    from robotsix_mill.runtime import tracing

    settings = _make_settings(tmp_path)
    seen = {}

    @contextlib.contextmanager
    def fake_root(sid, name=None):
        seen["session_id"] = sid
        seen["stage"] = name
        yield

    def mock_agent(**kwargs):
        seen["agent_ran_under"] = seen.get("session_id")
        return env_syncing.EnvSyncResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(tracing, "start_ticket_root_span", fake_root)
    monkeypatch.setattr(env_syncing, "run_env_sync_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )

    res = run_env_sync_pass()

    assert res.session_id.startswith("env-sync-")
    assert seen["session_id"] == res.session_id
    assert seen["stage"] == "env-sync"
    assert seen["agent_ran_under"] == res.session_id


def test_env_sync_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    """Session IDs are unique across runs."""
    from robotsix_mill.agents import env_syncing

    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        env_syncing, "run_env_sync_agent",
        lambda **k: env_syncing.EnvSyncResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.env_sync_runner.Settings", lambda: settings
    )
    a = run_env_sync_pass().session_id
    b = run_env_sync_pass().session_id
    assert a != b and a.startswith("env-sync-") and b.startswith("env-sync-")
