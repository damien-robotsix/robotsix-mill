"""Tests for the module-curator agent and runner."""

from robotsix_mill.agents import module_curator as mc_agent
from robotsix_mill.runners.periodic_runner import (
    run_module_curator_pass,
    PeriodicPassResult,
)
from robotsix_mill.runners.pass_runner import _GAP_ID_RE


# --- Agent tests ---


def test_module_curator_system_prompt_covers_all_drift_classes():
    """The module-curator agent prompt must cover all four drift classes."""
    p = mc_agent.SYSTEM_PROMPT.lower()
    # 1. Unclassified files
    assert "unclassified" in p
    # 2. Stale paths
    assert "stale path" in p
    # 3. New module proposals
    assert "new module" in p
    # 4. Reorganization toward a per-module layout (c0fd)
    assert "reorganiz" in p
    assert "per-module" in p
    # The reorg proposal must be proactive and propose one module/group per ticket.
    assert "one module per ticket" in p
    # Module consolidation (grouping or merging similar modules) must be covered.
    assert "consolidat" in p
    # Must be read-only
    assert (
        "read-only" in p
        or "read only" in p
        or "do not move" in p
        or "do not delete" in p
    )
    # Must use the de-duplication guidance
    assert "de-duplication" in p or "deduplication" in p
    # Must reference docs/modules.yaml
    assert "docs/modules.yaml" in p or "modules.yaml" in p


def test_module_curator_result_model():
    """ModuleCuratorResult has the expected fields and defaults."""
    result = mc_agent.ModuleCuratorResult(
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
    default_result = mc_agent.ModuleCuratorResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_module_curator_result_field_types():
    """ModuleCuratorResult fields have correct types."""
    result = mc_agent.ModuleCuratorResult(
        updated_memory="# Module Curator Memory\n",
        draft_titles=["Classify file: assign to existing module or propose a new one"],
        draft_bodies=["The file src/foo.py is unclassified..."],
        gap_ids=["unclassified_foo"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)


def test_max_drafts_is_reasonable():
    """MAX_DRAFTS should be a positive integer."""
    assert isinstance(mc_agent.MAX_DRAFTS, int)
    assert mc_agent.MAX_DRAFTS > 0


def test_runner_stub_exists():
    """The runner stub should be callable with correct types."""
    assert callable(run_module_curator_pass)
    assert issubclass(PeriodicPassResult, object)


def test_gap_id_re_matches_module_curator():
    """The _GAP_ID_RE must match module_curator markers so de-duplication works."""
    marker = "<!-- module_curator-gap-id: unclassified_src_foo -->"
    matches = _GAP_ID_RE.findall(marker)
    assert len(matches) == 1
    label, gap_id = matches[0]
    assert label == "module_curator"
    assert gap_id == "unclassified_src_foo"


def test_gap_id_re_matches_bespoke():
    """Regression: bespoke:<name> labels used to be silently skipped by the
    hardcoded alternation regex, breaking dedup for every bespoke agent."""
    marker = "<!-- bespoke:my_agent-gap-id: abc123 -->"
    matches = _GAP_ID_RE.findall(marker)
    assert matches == [("bespoke:my_agent", "abc123")]


def test_gap_id_re_matches_trace_dash_health():
    marker = "<!-- trace-health-gap-id: xyz -->"
    matches = _GAP_ID_RE.findall(marker)
    assert matches == [("trace-health", "xyz")]


def test_gap_id_re_matches_trace_dash_review():
    marker = "<!-- trace-review-gap-id: pq -->"
    matches = _GAP_ID_RE.findall(marker)
    assert matches == [("trace-review", "pq")]


def test_gap_id_re_matches_all_legacy_labels():
    """All 11 labels the old alternation captured must still match."""
    for label in (
        "audit",
        "health",
        "agent_check",
        "retrospect",
        "survey",
        "test_gap",
        "bc_check",
        "config_sync",
        "completeness_check",
        "copy_paste",
        "module_curator",
    ):
        marker = f"<!-- {label}-gap-id: anchor -->"
        matches = _GAP_ID_RE.findall(marker)
        assert matches == [(label, "anchor")], f"failed for {label!r}"


def test_gap_id_re_rejects_malformed():
    """No leading label part → no match."""
    assert _GAP_ID_RE.findall("<!-- -gap-id: foo -->") == []
    assert _GAP_ID_RE.findall("<!-- gap-id: foo -->") == []


# ---------------------------------------------------------------------------
# CLI subcommand tests
# ---------------------------------------------------------------------------


def test_module_curator_cli_command(capsys, monkeypatch):
    """Test that CLI module-curator command works."""
    from robotsix_mill.cli import main
    from robotsix_mill.runners.periodic_runner import PeriodicPassResult

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Classify file: src/foo.py"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_module_curator_pass", mock_run
    )

    result = main(["module-curator"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Module-curator pass complete" in captured.out
    assert "Classify file" in captured.out


def test_module_curator_cli_json_output(capsys, monkeypatch):
    """Test JSON output flag for module-curator CLI."""
    import json

    from robotsix_mill.cli import main
    from robotsix_mill.runners.periodic_runner import PeriodicPassResult

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "456", "title": "Stale path: old/file.py"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_module_curator_pass", mock_run
    )

    result = main(["module-curator", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data
    assert data["tickets_created"] == [
        {"id": "456", "title": "Stale path: old/file.py"}
    ]


def test_module_curator_cli_no_drafts(capsys, monkeypatch):
    """CLI module-curator command when no drafts created."""
    from robotsix_mill.cli import main
    from robotsix_mill.runners.periodic_runner import PeriodicPassResult

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_module_curator_pass", mock_run
    )

    result = main(["module-curator"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No new draft tickets created" in captured.out


def test_module_curator_cli_failure(capsys, monkeypatch):
    """CLI module-curator exits 1 on failure."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        raise RuntimeError("module-curator exploded")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_module_curator_pass", mock_run
    )

    result = main(["module-curator"])
    assert result == 1
    captured = capsys.readouterr()
    assert "module-curator failed" in captured.err
