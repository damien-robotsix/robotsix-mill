"""Tests for the audit agent and runner."""

import json
import pytest
from pathlib import Path

from robotsix_mill.agents import auditing
from robotsix_mill.audit_runner import run_audit_pass, AuditPassResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    return Settings(**overrides)


# --- Agent tests ---


def test_audit_agent_result_model():
    """AuditResult has the expected fields."""
    result = auditing.AuditResult(
        updated_memory="memory",
        draft_titles=["title1"],
        draft_bodies=["body1"],
        gap_ids=["gap1"],
    )
    assert result.updated_memory == "memory"
    assert len(result.draft_titles) == 1
    assert len(result.draft_bodies) == 1
    assert len(result.gap_ids) == 1


# --- Runner tests ---


def test_run_audit_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return auditing.AuditResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.audit_runner.Settings", lambda: settings)

    run_audit_pass()
    assert captured_memory == [""]


def test_run_audit_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.audit_memory_file
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return auditing.AuditResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.audit_runner.Settings", lambda: settings)

    run_audit_pass()
    assert captured_memory == ["# Existing memory\n## Proposed\n- gap1\n"]


def test_run_audit_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- gap1\n"

    def mock_agent(**kwargs):
        return auditing.AuditResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.audit_runner.Settings", lambda: settings)

    run_audit_pass()
    memory_file = settings.audit_memory_file
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_audit_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap."""
    settings = _make_settings(tmp_path)
    db.init_db(settings)
    service = TicketService(settings)

    def mock_agent(**kwargs):
        return auditing.AuditResult(
            updated_memory="# Memory\n",
            draft_titles=["Fix gap1", "Fix gap2"],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["gap1", "gap2"],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.audit_runner.Settings", lambda: settings)

    result = run_audit_pass()
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="audit"
    tickets = service.list()
    audit_tickets = [t for t in tickets if t.source == "audit"]
    assert len(audit_tickets) == 2
    assert audit_tickets[0].state == State.DRAFT


def test_run_audit_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.init_db(settings)

    def mock_agent(**kwargs):
        return auditing.AuditResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.audit_runner.Settings", lambda: settings)

    result = run_audit_pass()
    assert len(result.drafts_created) == 0


def test_run_audit_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    # Ensure memory file does NOT exist
    memory_file = settings.audit_memory_file
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return auditing.AuditResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.audit_runner.Settings", lambda: settings)

    # Should not raise
    result = run_audit_pass()
    assert captured_memory == [""]


def test_audit_pass_result_structure(tmp_path, monkeypatch):
    """AuditPassResult has correct structure."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return auditing.AuditResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr("robotsix_mill.audit_runner.Settings", lambda: settings)

    result = run_audit_pass()
    assert isinstance(result, AuditPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1


# --- Config tests ---


def test_audit_config_defaults():
    """Audit config has correct defaults."""
    s = Settings()
    assert s.audit_periodic is False
    assert s.audit_interval_seconds == 3600
    assert s.audit_memory_path is None


def test_audit_memory_file_default(tmp_path):
    """When audit_memory_path is None, falls back to data_dir/audit_memory.md."""
    s = _make_settings(tmp_path)
    expected = s.data_dir / "audit_memory.md"
    assert s.audit_memory_file == expected


def test_audit_memory_file_override(tmp_path):
    """When audit_memory_path is set, uses that path."""
    custom_path = tmp_path / "custom_audit.md"
    s = _make_settings(tmp_path, MILL_AUDIT_MEMORY_PATH=str(custom_path))
    assert s.audit_memory_file == custom_path


# --- CLI tests ---


def test_audit_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI audit command works."""
    from robotsix_mill.cli import main

    # Mock the run_audit_pass function
    def mock_run(root=None):
        return AuditPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr("robotsix_mill.audit_runner.run_audit_pass", mock_run)

    result = main(["audit"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Audit pass complete" in captured.out


def test_audit_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag."""
    from robotsix_mill.cli import main

    def mock_run(root=None):
        return AuditPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr("robotsix_mill.audit_runner.run_audit_pass", mock_run)

    result = main(["audit", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data


def test_run_audit_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """Each audit run wraps the agent in a Langfuse session span with a
    unique per-run id, and returns it (so audit traces aren't
    untagged). No-op-safe when tracing isn't ready."""
    import contextlib

    from robotsix_mill.runtime import tracing

    settings = _make_settings(tmp_path)
    seen = {}

    @contextlib.contextmanager
    def fake_root(sid):
        seen["session_id"] = sid
        yield

    @contextlib.contextmanager
    def fake_stage(name):
        seen["stage"] = name
        yield

    def mock_agent(**kwargs):
        seen["agent_ran_under"] = seen.get("session_id")  # set before call
        return auditing.AuditResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(tracing, "start_ticket_root_span", fake_root)
    monkeypatch.setattr(tracing, "trace_stage", fake_stage)
    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.audit_runner.Settings", lambda: settings
    )

    res = run_audit_pass()

    assert res.session_id.startswith("audit-")
    assert seen["session_id"] == res.session_id          # span uses that id
    assert seen["stage"] == "audit"
    assert seen["agent_ran_under"] == res.session_id      # agent inside span


def test_audit_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        auditing, "run_audit_agent",
        lambda **k: auditing.AuditResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.audit_runner.Settings", lambda: settings
    )
    a = run_audit_pass().session_id
    b = run_audit_pass().session_id
    assert a != b and a.startswith("audit-") and b.startswith("audit-")


def test_run_audit_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the audit run clones the repo locally
    and hands the agent repo_dir (so it explores instead of
    web-fetching the project's files). Idempotent + best-effort."""
    from robotsix_mill.vcs import git_ops

    settings = _make_settings(
        tmp_path, FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )
    seen = {"clone": 0, "repo_dir": "unset"}

    def fake_clone(url, dest, branch, token):
        seen["clone"] += 1
        (dest / ".git").mkdir(parents=True)

    def mock_agent(**kwargs):
        seen["repo_dir"] = kwargs.get("repo_dir")
        return auditing.AuditResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.audit_runner.Settings", lambda: settings
    )

    run_audit_pass()
    repo = settings.data_dir / "audit_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo

    seen["clone"] = 0
    run_audit_pass()                       # reuse existing clone
    assert seen["clone"] == 0 and seen["repo_dir"] == repo


def test_run_audit_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)   # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        auditing, "run_audit_agent",
        lambda **k: got.__setitem__("repo_dir", k.get("repo_dir")) or
        auditing.AuditResult(updated_memory="m", draft_titles=[],
                             draft_bodies=[], gap_ids=[]),
    )
    monkeypatch.setattr(
        "robotsix_mill.audit_runner.Settings", lambda: settings
    )
    run_audit_pass()
    assert got["repo_dir"] is None
