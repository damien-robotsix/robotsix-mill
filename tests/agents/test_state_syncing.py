"""Tests for the state-sync agent."""

from pathlib import Path

from robotsix_mill.agents import state_syncing as state_sync_agent
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


def test_state_sync_system_prompt_covers_key_dimensions():
    """The state-sync agent prompt must cover key inspection dimensions."""
    p = state_sync_agent.SYSTEM_PROMPT.lower()
    for kw in (
        "states.py",
        "state",
        "enum",
        "stale",
        "typo",
        "memory",
    ):
        assert kw in p, f"state-sync prompt missing dimension cue: {kw}"
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p


def test_state_sync_result_model():
    """StateSyncResult has the expected fields and defaults."""
    result = state_sync_agent.StateSyncResult(
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
    default_result = state_sync_agent.StateSyncResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_state_sync_result_field_types():
    """StateSyncResult fields have correct types."""
    result = state_sync_agent.StateSyncResult(
        updated_memory="# State-Sync Memory\n",
        draft_titles=["state sync: stale — old_value"],
        draft_bodies=["Found stale reference in..."],
        gap_ids=["stale_old_value"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)


# --- Config tests ---


def test_state_sync_config_defaults():
    """State-sync config has correct defaults."""
    s = Settings()
    assert s.state_sync_periodic is True
    assert s.state_sync_interval_seconds == 604800


def test_state_sync_periodic_config():
    """State-sync periodic can be enabled/disabled and interval overridden."""
    s = Settings(state_sync_periodic="true", state_sync_interval_seconds="43200")
    assert s.state_sync_periodic is True
    assert s.state_sync_interval_seconds == 43200

    s2 = Settings(state_sync_periodic="false")
    assert s2.state_sync_periodic is False


# --- SourceKind tests ---


def test_sourcekind_has_state_sync():
    """SourceKind enum includes STATE_SYNC."""
    from robotsix_mill.core.models import SourceKind

    assert hasattr(SourceKind, "STATE_SYNC")
    assert SourceKind.STATE_SYNC == "state_sync"


def test_state_sync_in_periodic_pass_configs():
    """The state_sync entry exists in PERIODIC_PASS_CONFIGS."""
    from robotsix_mill.runners.periodic_runner import PERIODIC_PASS_CONFIGS

    assert "state_sync" in PERIODIC_PASS_CONFIGS
    cfg = PERIODIC_PASS_CONFIGS["state_sync"]
    assert cfg.label == "state_sync"
    assert cfg.agent_module_attr == "state_syncing"
    assert cfg.agent_fn_name == "run_state_sync_agent"
    assert cfg.requires_repo is True


def test_state_sync_in_builtin_kinds():
    """state_sync is listed in _BUILTIN_KINDS as mill_only."""
    from robotsix_mill.agents.periodic_loader import _BUILTIN_KINDS

    assert "state_sync" in _BUILTIN_KINDS
    assert _BUILTIN_KINDS["state_sync"] == "mill_only"


# --- Workflow portability gate (meta runner) ---


def test_is_internal_workflow_proposal_detects_state_sync():
    """_is_internal_workflow_proposal catches state_sync on a non-mill repo."""
    from robotsix_mill.meta.runner import _is_internal_workflow_proposal

    title = "Enable state_sync periodic workflow on cf82"
    body = (
        "Add a `.robotsix-mill/periodic/state_sync.yaml` presence file "
        "to enable state syncing on the cf82 repo."
    )
    result = _is_internal_workflow_proposal(title, body, "cf82")
    assert result == "state_sync"


def test_is_internal_workflow_proposal_allows_mill_repo():
    """Internal workflows ARE valid for robotsix-mill itself."""
    from robotsix_mill.meta.runner import _is_internal_workflow_proposal

    title = "Enable state_sync periodic workflow on mill"
    body = (
        "Add a `.robotsix-mill/periodic/state_sync.yaml` presence file "
        "to enable state syncing."
    )
    result = _is_internal_workflow_proposal(title, body, "robotsix-mill")
    assert result is None


def test_is_internal_workflow_proposal_allows_portable():
    """Portable workflows are allowed on any repo."""
    from robotsix_mill.meta.runner import _is_internal_workflow_proposal

    title = "Enable audit periodic workflow on cf82"
    body = (
        "Add a `.robotsix-mill/periodic/audit.yaml` presence file "
        "to enable auditing on the cf82 repo."
    )
    result = _is_internal_workflow_proposal(title, body, "cf82")
    assert result is None


def test_is_internal_workflow_proposal_no_match_returns_none():
    """Drafts without presence-file patterns return None."""
    from robotsix_mill.meta.runner import _is_internal_workflow_proposal

    result = _is_internal_workflow_proposal(
        "Add a new lint rule", "Update pyproject.toml", "cf82"
    )
    assert result is None


def test_is_internal_workflow_proposal_detects_frontend_sync():
    """frontend_sync (mill_only) is caught on non-mill repos."""
    from robotsix_mill.meta.runner import _is_internal_workflow_proposal

    body = "Create `.robotsix-mill/periodic/frontend_sync.yaml` on 7efb"
    result = _is_internal_workflow_proposal("", body, "7efb")
    assert result == "frontend_sync"


def test_state_sync_presence_file(tmp_path):
    """The per-repo presence file at .robotsix-mill/periodic/state_sync.yaml
    is well-formed."""
    import yaml

    repo_root = Path(__file__).parent.parent.parent
    presence = repo_root / ".robotsix-mill" / "periodic" / "state_sync.yaml"
    assert presence.exists(), f"Missing presence file: {presence}"
    data = yaml.safe_load(presence.read_text())
    assert data["name"] == "state_sync"
