"""Tests for the audit agent and runner."""

import json
from pathlib import Path

from robotsix_mill.agents import auditing
from robotsix_mill.runners.periodic_runner import run_audit_pass, AuditPassResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.core.models import SourceKind


def _test_repo_config():
    """Synthetic RepoConfig for periodic-runner tests — the runner now
    requires one (mono-repo board-less mode is gone)."""
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
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


# --- Agent tests ---


def test_audit_prompt_covers_codebase_health_and_tooling():
    """The audit YAML must weigh intrinsic codebase-health equally with
    external tooling. Per-repo specialisation (e.g. mill's DEFAULT
    MECHANISM RULE about proposing dedicated checker agents) lives in
    overlay files under <data_dir>/<board_id>/agent_overlays/audit.md
    so the core YAML stays repo-agnostic.

    Guard against silently reverting to tooling-only OR bleeding
    mill-specific assumptions back into the shipped YAML."""
    p = auditing.SYSTEM_PROMPT.lower()
    # Lens A: maintainability dimensions the user called out.
    for kw in (
        "maintainability",
        "oversized",
        "root",
        "readability",
        "docstring",
        "duplication",
        "list_dir",
        "synchronization",
    ):
        assert kw in p, f"audit prompt missing maintainability cue: {kw}"
    # Equal-weight framing, not tooling-only.
    assert "two complementary lenses" in p
    # Mill-isms must NOT appear in the shipped YAML — they belong in
    # the per-repo overlay for mill, not in the generic core.
    assert "default mechanism rule" not in p, (
        "DEFAULT MECHANISM RULE leaked back into shipped YAML; it should "
        "live in <data_dir>/robotsix-mill/agent_overlays/audit.md instead."
    )
    for mill_only in ("trace-health", "rebase/ci-fix", "ci-fix"):
        assert mill_only not in p, (
            f"mill-specific reference {mill_only!r} leaked into the "
            "shipped audit YAML — move it to mill's overlay file."
        )

    # Post-explore re-verification guardrails (compact checklist).
    assert "re-verification cap" in p, "audit prompt missing re-verification cap bullet"
    assert "≤ 3 total" in p, "audit prompt missing re-verification cap (≤ 3 total)"

    # parallel_explore fan-out guidance is now covered by the compact
    # tool-selection checklist (batch sub-questions; cap at 4).


def test_mill_ships_an_audit_overlay():
    """Mill's own audit overlay is git-tracked as a ``prompt_overlay:`` in its
    per-repo periodic-workflow file .robotsix-mill/periodic/audit.yaml, so a
    fresh mill deploy keeps the DEFAULT MECHANISM RULE behaviour (propose
    dedicated standing checker agents for recurring quality dimensions)
    without operator bootstrap. (Folded in from the former
    .robotsix-mill/agent_overlays/audit.md during the presence-file migration.)

    Guards against a future hand-edit that drops the mill-only meta-agent
    guidance — verified by resolving the merged definition."""
    from robotsix_mill.agents.periodic_loader import resolve_periodic_workflow

    audit_yaml = (
        Path(__file__).parent.parent.parent
        / ".robotsix-mill"
        / "periodic"
        / "audit.yaml"
    )
    assert audit_yaml.exists(), f"Expected mill's audit presence file at {audit_yaml}"
    resolved = resolve_periodic_workflow(audit_yaml)
    assert resolved is not None and resolved.kind == "llm_agent"
    body = resolved.definition.system_prompt  # built-in prompt + folded overlay
    # The DEFAULT MECHANISM RULE is the load-bearing overlay content.
    assert "DEFAULT MECHANISM RULE" in body
    assert "dedicated quality-checking agent" in body.lower() or (
        "dedicated standing" in body.lower()
    )


def test_run_audit_agent_wires_workflow_caller_audit(monkeypatch):
    """run_audit_agent forwards include_workflow_caller_audit=True (and the
    jscpd flag) through to run_periodic_agent."""
    captured: dict = {}

    def fake_run_periodic(**kwargs):
        captured["kwargs"] = kwargs
        return auditing.AuditResult()

    import robotsix_mill.agents.periodic_base as periodic_base

    monkeypatch.setattr(periodic_base, "run_periodic_agent", fake_run_periodic)

    auditing.run_audit_agent(settings=Settings())

    kw = captured["kwargs"]
    assert kw["include_workflow_caller_audit"] is True
    assert kw["include_jscpd"] is True


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
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_audit_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "audit_memory.md"
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
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
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
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
    memory_file = settings.data_dir / "test-repo" / "audit_memory.md"
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_audit_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap."""
    settings = _make_settings(tmp_path)
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return auditing.AuditResult(
            updated_memory="# Memory\n",
            draft_titles=["Fix gap1", "Fix gap2"],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["gap1", "gap2"],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="audit"
    tickets = service.list()
    audit_tickets = [t for t in tickets if t.source == SourceKind.AUDIT]
    assert len(audit_tickets) == 2
    assert audit_tickets[0].state == State.DRAFT
    # Each draft should have origin_session == the audit run's session_id.
    for t in audit_tickets:
        assert t.origin_session == result.session_id
        assert t.origin_session == "test-sid"


def test_run_audit_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return auditing.AuditResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 0


def test_run_audit_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    # Ensure memory file does NOT exist
    memory_file = settings.data_dir / "test-repo" / "audit_memory.md"
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
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    # Should not raise
    run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
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
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert isinstance(result, AuditPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1


# --- Config tests ---


def test_audit_config_defaults():
    """Audit config has correct defaults."""
    s = Settings()
    assert s.audit_periodic is True
    assert s.audit_interval_seconds == 86400


# --- CLI tests ---


def test_audit_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI audit command works."""
    from robotsix_mill.cli import main

    # Mock the run_audit_pass function
    def mock_run(session_id=None):
        return AuditPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_audit_pass", mock_run
    )

    result = main(["audit"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Audit pass complete" in captured.out


def test_audit_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return AuditPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_audit_pass", mock_run
    )

    result = main(["audit", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data


def test_run_audit_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """session_id is passed through to the result — tracing is now the
    poll loop's responsibility."""
    from robotsix_mill.agents import auditing

    settings = _make_settings(tmp_path)
    seen = {}

    def mock_agent(**kwargs):
        seen["agent_ran"] = True
        return auditing.AuditResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(auditing, "run_audit_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    res = run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())

    assert res.session_id == "test-sid"
    assert seen["agent_ran"] is True


def test_audit_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        auditing,
        "run_audit_agent",
        lambda **k: auditing.AuditResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    a = run_audit_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    ).session_id
    assert a == "test-sid"


def test_run_audit_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the audit run clones the repo locally
    and hands the agent repo_dir (so it explores instead of
    web-fetching the project's files). Idempotent + best-effort."""
    from robotsix_mill.vcs import git_ops

    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
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
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
    repo = settings.data_dir / "audit_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo

    seen["clone"] = 0
    run_audit_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )  # each run wipes + re-clones fresh (clean workspace)
    assert seen["clone"] == 1 and seen["repo_dir"] == repo  # re-clones fresh each run


def test_run_audit_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)  # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        auditing,
        "run_audit_agent",
        lambda **k: (
            got.__setitem__("repo_dir", k.get("repo_dir"))
            or auditing.AuditResult(
                updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
            )
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    run_audit_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert got["repo_dir"] is None
