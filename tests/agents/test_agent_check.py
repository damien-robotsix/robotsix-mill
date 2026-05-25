"""Tests for the agent-check agent and runner."""

import json
import pytest
from pathlib import Path

from robotsix_mill.agents import agent_check
from robotsix_mill.agent_check_runner import (
    run_agent_check_pass,
    AgentCheckPassResult,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def _make_settings(tmp_path, **overrides):
    """Create Settings with data_dir pointing to tmp_path."""
    overrides.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s)
    return s


def _empty_result():
    """Return a minimal no-gap AgentCheckResult for mock agents."""
    return agent_check.AgentCheckResult(
        findings="All good.",
        updated_memory="# Memory\n",
        draft_titles=[],
        draft_bodies=[],
    )


# --- Agent tests ---


def test_agent_check_prompt_covers_all_coherence_dimensions():
    """The agent-check prompt must cover all six coherence dimensions A-F
    and now targets YAML files instead of Python source."""
    p = agent_check.SYSTEM_PROMPT.lower()
    # Dimension A: Tool–Prompt Coherence
    for kw in ("tool–prompt", "backtick-quoted", "actual tool set", "mismatch"):
        assert kw in p, f"agent-check prompt missing tool-prompt cue: {kw}"
    # pydantic-ai auto-injection must be documented so the agent knows
    # not to flag absent tool mentions as gaps.
    assert "docstring_format" in p or "auto-injects" in p or (
        "pydantic-ai" in p and "auto" in p
    )
    # "tool in actual set but never mentioned" must NOT be treated as a gap.
    assert "do not flag" in p or "DO NOT flag" in p or "absence from the prompt" in p
    # Must mention docstring staleness / prompt-docstring contradiction checks.
    assert "docstring" in p and ("contradict" in p or "fs_tools.py" in p)
    # Dimension B: Skill Coherence
    for kw in ("skill", "frontmatter", "orphan"):
        assert kw in p, f"agent-check prompt missing skill cue: {kw}"
    # Dimension C: Metadata Correctness
    for kw in ("metadata", "report_issue", "name", "model", "duplicate"):
        assert kw in p, f"agent-check prompt missing metadata cue: {kw}"
    # Dimension D: Agent Registration Completeness
    for kw in ("registration", "_model", "orphan"):
        assert kw in p, f"agent-check prompt missing registration cue: {kw}"
    # Dimension E: Prompt Self-Consistency
    for kw in ("self-consistency", "copy-paste", "drift"):
        assert kw in p, f"agent-check prompt missing self-consistency cue: {kw}"
    # Dimension F: Memory Ledger Coherence
    for kw in ("memory ledger", "*_memory.md", "staleness", "reconciliation", "format consistency", "truncated"):
        assert kw in p, f"agent-check prompt missing memory-ledger cue: {kw}"
    assert "skip `agent_check_memory.md`" in p or "agent_check_memory.md" in p
    # Must reference YAML files and agent_definitions/
    assert "agent_definitions/" in p
    assert ".yaml" in p
    # Must use explore/read_file/list_dir tools
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p
    # Must read yaml_loader.py, base.py, config.py
    assert "yaml_loader.py" in p
    assert "base.py" in p
    assert "config.py" in p
    # Must read skills and references
    assert "skills/" in p or "SKILL.md" in p
    assert "agent_references/" in p
    # Memory note about deleted false-positive drafts
    assert "90ac" in p or "d847" in p or "false-positive" in p or (
        "deleted" in p and "draft" in p
    )
    # Must mention `${VAR}` references for model field
    assert "${var}" in p or "settings field" in p


def test_agent_check_result_model():
    """AgentCheckResult has the expected fields."""
    result = agent_check.AgentCheckResult(
        findings="All checks passed.",
        updated_memory="# Memory\n",
        draft_titles=["Fix gap1"],
        draft_bodies=["Body1"],
    )
    assert result.findings == "All checks passed."
    assert result.updated_memory == "# Memory\n"
    assert len(result.draft_titles) == 1
    assert len(result.draft_bodies) == 1


def test_agent_check_result_defaults():
    """AgentCheckResult defaults are sensible."""
    result = agent_check.AgentCheckResult()
    assert result.findings == ""
    assert result.updated_memory == ""
    assert result.draft_titles == []
    assert result.draft_bodies == []


def test_agent_check_result_field_types():
    """AgentCheckResult fields have correct types."""
    result = agent_check.AgentCheckResult(
        findings="Findings text",
        updated_memory="# Mem\n",
        draft_titles=["Title 1", "Title 2"],
        draft_bodies=["Body 1", "Body 2"],
    )
    assert isinstance(result.findings, str)
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)


# --- Runner tests ---


def test_run_agent_check_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _empty_result()

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    run_agent_check_pass()
    assert captured_memory == [""]


def test_run_agent_check_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.agent_check_memory_file
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text(
        "# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8"
    )

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _empty_result()

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    run_agent_check_pass()
    assert captured_memory == ["# Existing memory\n## Proposed\n- gap1\n"]


def test_run_agent_check_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- gap1\n"

    def mock_agent(**kwargs):
        return agent_check.AgentCheckResult(
            findings="done",
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
        )

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    run_agent_check_pass()
    memory_file = settings.agent_check_memory_file
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_agent_check_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets with source='agent_check'."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)
    service = TicketService(settings)

    def mock_agent(**kwargs):
        return agent_check.AgentCheckResult(
            findings="Found gaps.",
            updated_memory="# Memory\n",
            draft_titles=["Fix gap1", "Fix gap2"],
            draft_bodies=["Body1", "Body2"],
        )

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    result = run_agent_check_pass()
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="agent_check"
    tickets = service.list()
    ac_tickets = [t for t in tickets if t.source == "agent_check"]
    assert len(ac_tickets) == 2
    assert ac_tickets[0].state == State.DRAFT
    # Each draft should have origin_session == the run's session_id.
    for t in ac_tickets:
        assert t.origin_session == result.session_id
        assert t.origin_session.startswith("agent-check-")


def test_run_agent_check_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)

    def mock_agent(**kwargs):
        return _empty_result()

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    result = run_agent_check_pass()
    assert len(result.drafts_created) == 0


def test_run_agent_check_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    memory_file = settings.agent_check_memory_file
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _empty_result()

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    run_agent_check_pass()
    assert captured_memory == [""]


def test_agent_check_pass_result_structure(tmp_path, monkeypatch):
    """AgentCheckPassResult has correct structure."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return agent_check.AgentCheckResult(
            findings="done",
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
        )

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    result = run_agent_check_pass()
    assert isinstance(result, AgentCheckPassResult)
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


def test_run_agent_check_pass_skips_empty_title_or_body(tmp_path, monkeypatch):
    """Runner skips draft entries with empty title or body."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings)

    def mock_agent(**kwargs):
        return agent_check.AgentCheckResult(
            findings="done",
            updated_memory="mem",
            draft_titles=["Valid", "", "Also Valid"],
            draft_bodies=["Body", "Body2", ""],
        )

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    result = run_agent_check_pass()
    assert len(result.drafts_created) == 1  # only first has both title + body


# --- Config tests ---


def test_agent_check_config_defaults():
    """Agent-check config has correct defaults."""
    s = Settings()
    assert s.agent_check_model == "deepseek/deepseek-v4-pro"


def test_agent_check_config_custom_model():
    """Agent-check model can be overridden via env."""
    s = Settings(MILL_AGENT_CHECK_MODEL="anthropic/claude-sonnet-4")
    assert s.agent_check_model == "anthropic/claude-sonnet-4"


def test_agent_check_memory_file_default(tmp_path):
    """When agent_check_memory_path is None, falls back to
    data_dir/agent_check_memory.md."""
    s = _make_settings(tmp_path)
    expected = s.data_dir / "agent_check_memory.md"
    assert s.agent_check_memory_file == expected


def test_agent_check_memory_file_override(tmp_path):
    """When agent_check_memory_path is set, uses that path."""
    custom_path = tmp_path / "custom_agent_check.md"
    s = _make_settings(
        tmp_path, MILL_AGENT_CHECK_MEMORY_PATH=str(custom_path)
    )
    assert s.agent_check_memory_file == custom_path


def test_agent_check_memory_path_config():
    """Agent-check memory path can be set via env."""
    s = Settings()
    assert s.agent_check_memory_path is None


# --- CLI tests ---


def test_agent_check_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI agent-check command works."""
    from robotsix_mill.cli import main

    def mock_run(root=None):
        return AgentCheckPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.run_agent_check_pass", mock_run
    )

    result = main(["agent-check"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Agent-check pass complete" in captured.out
    assert "Fix gap" in captured.out


def test_agent_check_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag for agent-check CLI."""
    from robotsix_mill.cli import main

    def mock_run(root=None):
        return AgentCheckPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.run_agent_check_pass", mock_run
    )

    result = main(["agent-check", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data
    assert data["tickets_created"] == [{"id": "123", "title": "Fix gap"}]


def test_agent_check_cli_no_drafts(capsys, tmp_path, monkeypatch):
    """CLI agent-check command when no drafts created."""
    from robotsix_mill.cli import main

    def mock_run(root=None):
        return AgentCheckPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.run_agent_check_pass", mock_run
    )

    result = main(["agent-check"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No new draft tickets created" in captured.out


def test_agent_check_cli_failure(capsys, monkeypatch):
    """CLI agent-check exits 1 on failure."""
    from robotsix_mill.cli import main

    def mock_run(root=None):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.run_agent_check_pass", mock_run
    )

    result = main(["agent-check"])
    assert result == 1
    captured = capsys.readouterr()
    assert "agent-check failed" in captured.err


# --- Langfuse session tests ---


def test_run_agent_check_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """Each agent-check run wraps the agent in a Langfuse session span
    with a unique per-run id. No-op-safe when tracing isn't ready."""
    import contextlib

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
        return _empty_result()

    monkeypatch.setattr(tracing, "start_ticket_root_span", fake_root)
    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    res = run_agent_check_pass()

    assert res.session_id.startswith("agent-check-")
    assert seen["session_id"] == res.session_id
    assert seen["stage"] == "agent-check"
    assert seen["agent_ran_under"] == res.session_id


def test_agent_check_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        agent_check, "run_agent_check_agent",
        lambda **k: _empty_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )
    a = run_agent_check_pass().session_id
    b = run_agent_check_pass().session_id
    assert a != b and a.startswith("agent-check-") and b.startswith("agent-check-")


# --- Clone tests ---


def test_run_agent_check_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the agent-check run clones the repo
    locally and hands the agent repo_dir and memory_dir. Idempotent + best-effort."""
    from robotsix_mill.vcs import git_ops

    settings = _make_settings(
        tmp_path, FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )
    seen = {"clone": 0, "repo_dir": "unset", "memory_dir": "unset"}

    def fake_clone(url, dest, branch, token):
        seen["clone"] += 1
        (dest / ".git").mkdir(parents=True)

    def mock_agent(**kwargs):
        seen["repo_dir"] = kwargs.get("repo_dir")
        seen["memory_dir"] = kwargs.get("memory_dir")
        return _empty_result()

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )

    run_agent_check_pass()
    repo = settings.data_dir / "agent_check_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo
    assert seen["memory_dir"] == settings.data_dir

    seen["clone"] = 0
    run_agent_check_pass()
    assert seen["clone"] == 0 and seen["repo_dir"] == repo
    assert seen["memory_dir"] == settings.data_dir


def test_run_agent_check_agent_passes_extra_roots(monkeypatch):
    """When both repo_dir and memory_dir are provided,
    build_fs_tools is called with extra_roots=[memory_dir]."""
    from robotsix_mill.agents import fs_tools, explore, base, retry

    captured_extra_roots = None
    settings = Settings()

    def _fake_read(path, *, offset=1, limit=None):
        return "content"

    def _fake_list(path="."):
        return "entries"

    _fake_read.__name__ = "read_file"
    _fake_list.__name__ = "list_dir"

    def fake_build_fs_tools(root, s, *, extra_roots=None):
        nonlocal captured_extra_roots
        captured_extra_roots = extra_roots
        return [_fake_read, _fake_list]

    def fake_explore_tool(s, repo_dir):
        async def _explore(ctx, question):
            return "answer"
        _explore.__name__ = "explore"
        return _explore

    class FakeRunResult:
        output = agent_check.AgentCheckResult(
            findings="ok",
            updated_memory="mem",
        )

    class FakeAgent:
        def run_sync(self, prompt):
            return FakeRunResult()

    def fake_build_agent(*args, **kwargs):
        return FakeAgent()

    def fake_call_with_retry(fn, *, settings=None, what=""):
        return fn()

    monkeypatch.setattr(fs_tools, "build_fs_tools", fake_build_fs_tools)
    monkeypatch.setattr(explore, "make_explore_tool", fake_explore_tool)
    monkeypatch.setattr(base, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(retry, "call_with_retry", fake_call_with_retry)

    agent_check.run_agent_check_agent(
        settings=settings,
        repo_dir=Path("/fake/repo"),
        memory_dir=Path("/fake/data"),
    )

    assert captured_extra_roots == [Path("/fake/data")]


def test_run_agent_check_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)   # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        agent_check, "run_agent_check_agent",
        lambda **k: got.__setitem__("repo_dir", k.get("repo_dir")) or
        _empty_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.agent_check_runner.Settings", lambda: settings
    )
    run_agent_check_pass()
    assert got["repo_dir"] is None
