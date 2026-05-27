"""Tests for the env-sync agent."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from robotsix_mill.agents import env_syncing


def _wrap_retry(fn, **kwargs):
    """Simulate call_with_retry returning an object with .output."""
    return SimpleNamespace(output=fn())


# --- Agent tests ---


def test_env_sync_system_prompt_covers_key_dimensions():
    """The env-sync agent prompt must cover key inspection dimensions."""
    p = env_syncing.SYSTEM_PROMPT.lower()
    for kw in (
        "config.py", ".env", "docs/configuration.md",
        "missing", "stale", "drifted",
        "memory",
    ):
        assert kw in p, f"env-sync prompt missing dimension cue: {kw}"
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p
    # Must NOT use web research.
    assert "web" not in p or "no web" in p


def test_env_sync_result_model():
    """EnvSyncResult has the expected fields and defaults."""
    result = env_syncing.EnvSyncResult(
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
    default_result = env_syncing.EnvSyncResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_env_sync_result_field_types():
    """EnvSyncResult fields have correct types."""
    result = env_syncing.EnvSyncResult(
        updated_memory="# Env-Sync Memory\n",
        draft_titles=["env drift: FOO missing from .env"],
        draft_bodies=["Alias FOO is in config.py..."],
        gap_ids=["foo_missing_env"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)


def test_run_env_sync_agent_web_false(monkeypatch):
    """env-sync agent is constructed with web=False."""
    from robotsix_mill.config import Settings

    build_calls = []

    def fake_build_agent(settings, **kwargs):
        build_calls.append(kwargs)
        from unittest.mock import MagicMock
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = env_syncing.EnvSyncResult()
        return mock_agent

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    monkeypatch.setattr("robotsix_mill.agents.retry.call_with_retry", _wrap_retry)

    s = Settings(MILL_DATA_DIR="/tmp/test_env_sync")
    env_syncing.run_env_sync_agent(settings=s, memory="")

    assert len(build_calls) == 1
    assert build_calls[0]["web"] is False
    assert build_calls[0]["name"] == "env-sync"
    assert build_calls[0]["model_name"] == s.env_sync_model


def test_run_env_sync_agent_max_gaps_clipping(monkeypatch):
    """Draft titles/bodies/gap_ids are clipped to MAX_GAPS."""
    from robotsix_mill.config import Settings

    def fake_build_agent(settings, **kwargs):
        from unittest.mock import MagicMock
        result = env_syncing.EnvSyncResult(
            draft_titles=[f"title{i}" for i in range(10)],
            draft_bodies=[f"body{i}" for i in range(10)],
            gap_ids=[f"gap{i}" for i in range(10)],
        )
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = result
        return mock_agent

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    monkeypatch.setattr("robotsix_mill.agents.retry.call_with_retry", _wrap_retry)

    s = Settings(MILL_DATA_DIR="/tmp/test_env_sync")
    result = env_syncing.run_env_sync_agent(settings=s, memory="")

    assert len(result.draft_titles) == env_syncing.MAX_GAPS
    assert len(result.draft_bodies) == env_syncing.MAX_GAPS
    assert len(result.gap_ids) == env_syncing.MAX_GAPS


def test_run_env_sync_agent_no_repo_dir_no_tools(monkeypatch):
    """Without repo_dir, agent is called with empty tools list."""
    from robotsix_mill.config import Settings

    build_calls = []

    def fake_build_agent(settings, **kwargs):
        build_calls.append(kwargs)
        from unittest.mock import MagicMock
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = env_syncing.EnvSyncResult()
        return mock_agent

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    monkeypatch.setattr("robotsix_mill.agents.retry.call_with_retry", _wrap_retry)

    s = Settings(MILL_DATA_DIR="/tmp/test_env_sync")
    env_syncing.run_env_sync_agent(settings=s, memory="")

    assert len(build_calls) == 1
    assert build_calls[0]["tools"] == []


def test_run_env_sync_agent_with_repo_dir_adds_tools(monkeypatch, tmp_path):
    """With repo_dir, agent gets read_file, list_dir, and explore tools."""
    from robotsix_mill.config import Settings

    build_calls = []

    def fake_build_agent(settings, **kwargs):
        build_calls.append(kwargs)
        from unittest.mock import MagicMock
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = env_syncing.EnvSyncResult()
        return mock_agent

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    monkeypatch.setattr("robotsix_mill.agents.retry.call_with_retry", _wrap_retry)

    # Create a minimal repo structure so build_fs_tools doesn't fail.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config.py").write_text("")

    s = Settings(MILL_DATA_DIR=str(tmp_path))
    env_syncing.run_env_sync_agent(settings=s, memory="", repo_dir=repo)

    assert len(build_calls) == 1
    tools = build_calls[0]["tools"]
    assert len(tools) >= 2  # explore + at least one fs tool


# --- Config tests ---


def test_env_sync_config_defaults():
    """Env-sync config has correct defaults."""
    from robotsix_mill.config import Settings
    s = Settings()
    assert s.env_sync_model == "openai/gpt-4o-mini"
    assert s.env_sync_periodic is True
    assert s.env_sync_interval_seconds == 86400
    assert s.env_sync_memory_path is None


def test_env_sync_config_custom_model():
    """Env-sync model can be overridden via env."""
    from robotsix_mill.config import Settings
    s = Settings(MILL_ENV_SYNC_MODEL="anthropic/claude-sonnet-4")
    assert s.env_sync_model == "anthropic/claude-sonnet-4"


def test_env_sync_memory_file_default(tmp_path):
    """When env_sync_memory_path is None, falls back to
    data_dir/env_sync_memory.md."""
    from robotsix_mill.config import Settings
    s = Settings(MILL_DATA_DIR=str(tmp_path))
    expected = s.data_dir / "env_sync_memory.md"
    assert s.env_sync_memory_file == expected


def test_env_sync_memory_file_override(tmp_path):
    """When env_sync_memory_path is set, uses that path."""
    from robotsix_mill.config import Settings
    custom_path = tmp_path / "custom_env_sync.md"
    s = Settings(MILL_DATA_DIR=str(tmp_path), MILL_ENV_SYNC_MEMORY_PATH=str(custom_path))
    assert s.env_sync_memory_file == custom_path


def test_env_sync_periodic_config():
    """Env-sync periodic can be enabled."""
    from robotsix_mill.config import Settings
    s = Settings(MILL_ENV_SYNC_PERIODIC="true", MILL_ENV_SYNC_INTERVAL_SECONDS="43200")
    assert s.env_sync_periodic is True
    assert s.env_sync_interval_seconds == 43200
