"""Tests for the config-sync agent."""

from types import SimpleNamespace


from robotsix_mill.agents import config_syncing


def _wrap_retry(agent, make_run, **kwargs):
    """Simulate run_agent: run make_run on the (mock) handle, wrap in .output."""
    return SimpleNamespace(output=make_run(agent))


# --- Agent tests ---


def test_config_sync_system_prompt_covers_key_dimensions():
    """The config-sync agent prompt must cover key inspection dimensions."""
    p = config_syncing.SYSTEM_PROMPT.lower()
    for kw in (
        "config.py",
        "config.example.yaml",
        "repos.example.yaml",
        "docs/configuration.md",
        "missing-from-yaml",
        "stale-yaml-key",
        "default-mismatch",
        "memory",
    ):
        assert kw in p, f"config-sync prompt missing dimension cue: {kw}"
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p
    # Must NOT use web research.
    assert "web" not in p or "no web" in p


def test_config_sync_result_model():
    """ConfigSyncResult has the expected fields and defaults."""
    result = config_syncing.ConfigSyncResult(
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
    default_result = config_syncing.ConfigSyncResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_config_sync_result_field_types():
    """ConfigSyncResult fields have correct types."""
    result = config_syncing.ConfigSyncResult(
        updated_memory="# Config-Sync Memory\n",
        draft_titles=["config drift: FOO missing from .env"],
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


def test_run_config_sync_agent_web_false(monkeypatch):
    """config-sync agent is constructed with web_knowledge=False."""
    from robotsix_mill.config import Settings

    build_calls = []

    def fake_build_agent(settings, **kwargs):
        build_calls.append(kwargs)
        from unittest.mock import MagicMock

        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = config_syncing.ConfigSyncResult()
        return mock_agent

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _wrap_retry)

    s = Settings(data_dir="/tmp/test_config_sync")
    config_syncing.run_config_sync_agent(settings=s, memory="")

    assert len(build_calls) == 1
    assert build_calls[0]["web_knowledge"] is False
    assert build_calls[0]["name"] == "config-sync"
    assert build_calls[0]["level"] == 1


def test_run_config_sync_agent_max_gaps_clipping(monkeypatch):
    """Draft titles/bodies/gap_ids are clipped to MAX_GAPS."""
    from robotsix_mill.config import Settings

    def fake_build_agent(settings, **kwargs):
        from unittest.mock import MagicMock

        result = config_syncing.ConfigSyncResult(
            draft_titles=[f"title{i}" for i in range(10)],
            draft_bodies=[f"body{i}" for i in range(10)],
            gap_ids=[f"gap{i}" for i in range(10)],
        )
        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = result
        return mock_agent

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _wrap_retry)

    s = Settings(data_dir="/tmp/test_config_sync")
    result = config_syncing.run_config_sync_agent(settings=s, memory="")

    assert len(result.draft_titles) == config_syncing.MAX_GAPS
    assert len(result.draft_bodies) == config_syncing.MAX_GAPS
    assert len(result.gap_ids) == config_syncing.MAX_GAPS


def test_run_config_sync_agent_no_repo_dir_no_tools(monkeypatch):
    """Without repo_dir, agent is called with empty tools list."""
    from robotsix_mill.config import Settings

    build_calls = []

    def fake_build_agent(settings, **kwargs):
        build_calls.append(kwargs)
        from unittest.mock import MagicMock

        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = config_syncing.ConfigSyncResult()
        return mock_agent

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _wrap_retry)

    s = Settings(data_dir="/tmp/test_config_sync")
    config_syncing.run_config_sync_agent(settings=s, memory="")

    assert len(build_calls) == 1
    assert build_calls[0]["tools"] == []


def test_run_config_sync_agent_with_repo_dir_adds_tools(monkeypatch, tmp_path):
    """With repo_dir, agent gets read_file, list_dir, and explore tools."""
    from robotsix_mill.config import Settings

    build_calls = []

    def fake_build_agent(settings, **kwargs):
        build_calls.append(kwargs)
        from unittest.mock import MagicMock

        mock_agent = MagicMock()
        mock_agent.run_sync.return_value = config_syncing.ConfigSyncResult()
        return mock_agent

    monkeypatch.setattr("robotsix_mill.agents.base.build_agent", fake_build_agent)
    monkeypatch.setattr("robotsix_mill.agents.retry.run_agent", _wrap_retry)

    # Create a minimal repo structure so build_fs_tools doesn't fail.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "config.py").write_text("")

    s = Settings(data_dir=str(tmp_path))
    config_syncing.run_config_sync_agent(settings=s, memory="", repo_dir=repo)

    assert len(build_calls) == 1
    tools = build_calls[0]["tools"]
    assert len(tools) >= 2  # explore + at least one fs tool


# --- Config tests ---


def test_config_sync_config_defaults():
    """Config-sync config has correct defaults."""
    from robotsix_mill.config import Settings

    s = Settings()
    assert s.config_sync_periodic is True
    assert s.config_sync_interval_seconds == 86400
    assert s.config_sync_memory_path is None


def test_config_sync_memory_file_default(tmp_path):
    """When config_sync_memory_path is None, falls back to
    data_dir/config_sync_memory.md."""
    from robotsix_mill.config import Settings

    s = Settings(data_dir=str(tmp_path))
    expected = s.data_dir / "config_sync_memory.md"
    assert s.config_sync_memory_file == expected


def test_config_sync_memory_file_override(tmp_path):
    """When config_sync_memory_path is set, uses that path."""
    from robotsix_mill.config import Settings

    custom_path = tmp_path / "custom_config_sync.md"
    s = Settings(data_dir=str(tmp_path), config_sync_memory_path=str(custom_path))
    assert s.config_sync_memory_file == custom_path


def test_config_sync_periodic_config():
    """Config-sync periodic can be enabled."""
    from robotsix_mill.config import Settings

    s = Settings(config_sync_periodic="true", config_sync_interval_seconds="43200")
    assert s.config_sync_periodic is True
    assert s.config_sync_interval_seconds == 43200
