"""Tests for the frontend-sync agent."""

from robotsix_mill.agents import frontend_syncing


# --- Agent tests ---


def test_frontend_sync_system_prompt_covers_key_dimensions():
    """The frontend-sync agent prompt must cover key inspection dimensions."""
    p = frontend_syncing.SYSTEM_PROMPT.lower()
    for kw in (
        "states.py",
        "models.py",
        "board-mill.css",
        "board-mill.js",
        "state",
        "sourcekind",
        "source_class",
        "state_trace",
        "agent_colors",
        "css",
        "class",
        "stale",
        "missing",
    ):
        assert kw in p, f"frontend-sync prompt missing dimension cue: {kw}"
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p


def test_frontend_sync_result_model():
    """FrontendSyncResult has the expected fields and defaults."""
    result = frontend_syncing.FrontendSyncResult(
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
    default_result = frontend_syncing.FrontendSyncResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_frontend_sync_result_field_types():
    """FrontendSyncResult fields have correct types."""
    result = frontend_syncing.FrontendSyncResult(
        updated_memory="# Frontend-Sync Memory\n",
        draft_titles=["frontend sync: missing CSS class — s-draft"],
        draft_bodies=["Found missing CSS class in..."],
        gap_ids=["missing_s_draft"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)
