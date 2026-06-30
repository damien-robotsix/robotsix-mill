"""Tests for the security-posture agent."""

from robotsix_mill.agents import security_posturing


# --- Agent tests ---


def test_security_posture_system_prompt_covers_key_dimensions():
    """The security-posture agent prompt must cover key inspection dimensions."""
    p = security_posturing.SYSTEM_PROMPT.lower()
    for kw in (
        "workflows",
        "pre-commit",
        "pyproject.toml",
        "owasp",
        "slsa",
        "sbom",
        "scorecard",
        "sast",
        "dast",
        "uv.lock",
    ):
        assert kw in p, f"security-posture prompt missing dimension cue: {kw}"
    # Must use explore/read_file/list_dir/run_command tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p
    assert "run_command" in p


def test_security_posture_result_model():
    """SecurityPostureResult has the expected fields and defaults."""
    result = security_posturing.SecurityPostureResult(
        updated_memory="memory",
        summary="summary",
        draft_titles=["title1"],
        draft_bodies=["body1"],
        gap_ids=["gap1"],
        verified_gap_ids=["gap1"],
    )
    assert result.updated_memory == "memory"
    assert result.summary == "summary"
    assert len(result.draft_titles) == 1
    assert len(result.draft_bodies) == 1
    assert len(result.gap_ids) == 1
    assert len(result.verified_gap_ids) == 1

    # Defaults
    default_result = security_posturing.SecurityPostureResult()
    assert default_result.updated_memory == ""
    assert default_result.summary == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []
    assert default_result.verified_gap_ids == []


def test_security_posture_result_field_types():
    """SecurityPostureResult fields have correct types."""
    result = security_posturing.SecurityPostureResult(
        updated_memory="# Security-Posture Memory\n",
        summary="Found 3 gaps and propose 2 tickets.",
        draft_titles=["Add CodeQL SAST workflow"],
        draft_bodies=["The repo lacks CodeQL..."],
        gap_ids=["missing_codeql"],
        verified_gap_ids=[],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.summary, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert isinstance(result.verified_gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)
    assert all(isinstance(g, str) for g in result.verified_gap_ids)
