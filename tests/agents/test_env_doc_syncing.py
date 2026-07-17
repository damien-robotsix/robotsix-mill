"""Tests for the env-doc-sync agent."""

from pathlib import Path

from robotsix_mill.agents import env_doc_syncing as env_doc_sync_agent
from robotsix_mill.config import Settings
from robotsix_mill.core import db


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    return s


# --- Agent tests ---


def test_env_doc_sync_system_prompt_covers_key_dimensions():
    """The env-doc-sync agent prompt must cover key inspection dimensions."""
    p = env_doc_sync_agent.SYSTEM_PROMPT.lower()
    for kw in (
        "configuration.md",
        "env",
        "settings",
        "docs",
        "memory",
    ):
        assert kw in p, f"env-doc-sync prompt missing dimension cue: {kw}"
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p


def test_env_doc_sync_result_model():
    """EnvDocSyncResult has the expected fields and defaults."""
    result = env_doc_sync_agent.EnvDocSyncResult(
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
    default_result = env_doc_sync_agent.EnvDocSyncResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_env_doc_sync_result_field_types():
    """EnvDocSyncResult fields have correct types."""
    result = env_doc_sync_agent.EnvDocSyncResult(
        updated_memory="# Env-Doc-Sync Memory\n",
        draft_titles=["env doc sync: missing-from-docs — MILL_FOO"],
        draft_bodies=["Found undocumented env var in..."],
        gap_ids=["missing_mill_foo"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)


# --- Config tests ---


def test_env_doc_sync_config_defaults():
    """Env-doc-sync config has correct defaults."""
    s = Settings()
    assert s.env_doc_sync_periodic is True
    assert s.env_doc_sync_interval_seconds == 604800


def test_env_doc_sync_periodic_config():
    """Env-doc-sync periodic can be enabled/disabled and interval overridden."""
    s = Settings(env_doc_sync_periodic="true", env_doc_sync_interval_seconds="43200")
    assert s.env_doc_sync_periodic is True
    assert s.env_doc_sync_interval_seconds == 43200

    s2 = Settings(env_doc_sync_periodic="false")
    assert s2.env_doc_sync_periodic is False


# --- SourceKind tests ---


def test_sourcekind_has_env_doc_sync():
    """SourceKind enum includes ENV_DOC_SYNC."""
    from robotsix_mill.core.models import SourceKind

    assert hasattr(SourceKind, "ENV_DOC_SYNC")
    assert SourceKind.ENV_DOC_SYNC == "env_doc_sync"


def test_env_doc_sync_in_periodic_pass_configs():
    """The env_doc_sync entry exists in PERIODIC_PASS_CONFIGS."""
    from robotsix_mill.runners.periodic_runner import PERIODIC_PASS_CONFIGS

    assert "env_doc_sync" in PERIODIC_PASS_CONFIGS
    cfg = PERIODIC_PASS_CONFIGS["env_doc_sync"]
    assert cfg.label == "env_doc_sync"
    assert cfg.agent_module_attr == "env_doc_syncing"
    assert cfg.agent_fn_name == "run_env_doc_sync_agent"
    assert cfg.requires_repo is True


def test_env_doc_sync_in_builtin_kinds():
    """env_doc_sync is listed in _BUILTIN_KINDS as mill_only."""
    from robotsix_mill.agents.periodic_loader import _BUILTIN_KINDS

    assert "env_doc_sync" in _BUILTIN_KINDS
    assert _BUILTIN_KINDS["env_doc_sync"] == "mill_only"


def test_env_doc_sync_presence_file(tmp_path):
    """The per-repo presence file at .robotsix-mill/periodic/env_doc_sync.yaml
    is well-formed."""
    import yaml

    repo_root = Path(__file__).parent.parent.parent
    presence = repo_root / ".robotsix-mill" / "periodic" / "env_doc_sync.yaml"
    assert presence.exists(), f"Missing presence file: {presence}"
    data = yaml.safe_load(presence.read_text())
    assert data["name"] == "env_doc_sync"
