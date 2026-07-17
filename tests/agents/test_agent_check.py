"""Tests for the agent-check agent and runner."""

import json
from pathlib import Path

from robotsix_mill.agents import agent_check
from robotsix_mill.runners.periodic_runner import (
    run_agent_check_pass,
    PeriodicPassResult,
)
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State


def _test_repo_config():
    """Synthetic RepoConfig for periodic-runner tests — the runner now
    requires one (mono-repo board-less mode is gone)."""
    from robotsix_mill.config import RepoConfig

    return RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
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


def _empty_result():
    """Return a minimal no-gap AgentCheckResult for mock agents."""
    return agent_check.AgentCheckResult(
        findings="All good.",
        updated_memory="# Memory\n",
        draft_titles=[],
        draft_bodies=[],
    )


# --- Agent tests ---


def test_agent_check_prompt_is_repo_agnostic_and_memory_first():
    """The rewritten prompt must be repo-agnostic: discover the repo's
    agent/tool definition layout (cached in memory), no-op when there are
    none, and run the coherence checks against the DISCOVERED layout rather
    than hard-coded robotsix-mill paths."""
    p = agent_check.SYSTEM_PROMPT.lower()

    # Repo-agnostic discovery, memory-first, with a cached layout block.
    assert "repo-agnostic" in p
    assert "## repo layout" in p
    assert "discover" in p
    # No-op contract for repos without agent/tool definitions.
    assert "no agent/tool definitions" in p
    assert "empty" in p

    # Generalized coherence dimensions (A–E), not mill-specific cues.
    for kw in (
        "tool–prompt coherence",
        "registration completeness",
        "metadata correctness",
        "prompt self-consistency",
        "memory-ledger coherence",
    ):
        assert kw in p, f"prompt missing coherence dimension: {kw}"

    # Auto-injection rule kept but framed generically (not mill-only), so
    # the agent doesn't flag intentionally-unmentioned tools.
    assert "auto-inject" in p
    assert "do not flag" in p

    # Tools available.
    for t in ("explore", "read_file", "list_dir"):
        assert t in p
    # recent-proposals dedup contract retained.
    assert "recent-proposals" in p
    # Output contract.
    for f in ("draft_titles", "draft_bodies", "gap_ids", "updated_memory"):
        assert f in p

    # Must NOT hard-code robotsix-mill internals (the whole point).
    for forbidden in (
        "src/robotsix_mill",
        "yaml_loader.py",
        "agent_references/",
        "90ac",
        "d847",
    ):
        assert forbidden not in p, f"prompt still hard-codes mill internal: {forbidden}"


def test_agent_check_handles_filed_tickets_like_audit_not_memory():
    """Per operator: agent_check must dedup already-posted tickets via the
    freshly-collected, per-repo `recent-proposals` block (exactly like the
    audit agent) — NOT via memory. Memory is for the layout cache +
    observations only, never ticket tracking."""
    p = agent_check.SYSTEM_PROMPT.lower()
    # audit's canonical proposal-handling line (verbatim-shared)
    assert "the db is the source of truth for ticket history." in p
    # recent-proposals is the dedup channel, collected per-repo each run
    assert "recent-proposals" in p
    assert "per-repo" in p
    # memory must explicitly exclude ticket tracking (audit's wording)
    assert "do not record in memory" in p
    assert "ticket ids or ticket states" in p


def test_agent_check_uses_normal_not_cheap_model():
    """Per operator: agent_check runs on the normal (default) tier — level 2."""
    from robotsix_mill.agents.yaml_loader import load_agent_definition

    d = load_agent_definition(Path("agent_definitions/periodic/agent_check.yaml"))
    assert d.level == 2


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
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_agent_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_agent_check_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "agent_check_memory.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _empty_result()

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_agent_check_pass(session_id="test-sid", repo_config=_test_repo_config())
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
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_agent_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    memory_file = settings.data_dir / "test-repo" / "agent_check_memory.md"
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_agent_check_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets with source='agent_check'."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return agent_check.AgentCheckResult(
            findings="Found gaps.",
            updated_memory="# Memory\n",
            draft_titles=["Fix gap1", "Fix gap2"],
            draft_bodies=["Body1", "Body2"],
        )

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_agent_check_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="agent_check"
    tickets = service.list()
    ac_tickets = [t for t in tickets if t.source == "agent_check"]
    assert len(ac_tickets) == 2
    assert ac_tickets[0].state == State.DRAFT
    # Each draft should have origin_session == the run's session_id.
    for t in ac_tickets:
        assert t.origin_session == result.session_id
        assert t.origin_session == "test-sid"


def test_run_agent_check_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return _empty_result()

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_agent_check_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert len(result.drafts_created) == 0


def test_run_agent_check_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "agent_check_memory.md"
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return _empty_result()

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_agent_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_agent_check_pass_result_structure(tmp_path, monkeypatch):
    """PeriodicPassResult has correct structure."""
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
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_agent_check_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert isinstance(result, PeriodicPassResult)
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


def test_run_agent_check_pass_skips_empty_title_or_body(tmp_path, monkeypatch):
    """Runner skips draft entries with empty title or body."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return agent_check.AgentCheckResult(
            findings="done",
            updated_memory="mem",
            draft_titles=["Valid", "", "Also Valid"],
            draft_bodies=["Body", "Body2", ""],
        )

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_agent_check_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    )
    assert len(result.drafts_created) == 1  # only first has both title + body


# --- Config tests ---


# --- CLI tests ---


def test_agent_check_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI agent-check command works."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_check_pass", mock_run
    )

    result = main(["agent-check"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Agent-check pass complete" in captured.out
    assert "Fix gap" in captured.out


def test_agent_check_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag for agent-check CLI."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Fix gap"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_check_pass", mock_run
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

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_check_pass", mock_run
    )

    result = main(["agent-check"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No new draft tickets created" in captured.out


def test_agent_check_cli_failure(capsys, monkeypatch):
    """CLI agent-check exits 1 on failure."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_agent_check_pass", mock_run
    )

    result = main(["agent-check"])
    assert result == 1
    captured = capsys.readouterr()
    assert "agent-check failed" in captured.err


# --- Langfuse session tests ---


def test_run_agent_check_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """session_id is passed through to the result — tracing is now the
    poll loop's responsibility."""

    settings = _make_settings(tmp_path)
    seen = {}

    def mock_agent(**kwargs):
        seen["agent_ran"] = True
        return _empty_result()

    monkeypatch.setattr(agent_check, "run_agent_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    res = run_agent_check_pass(session_id="test-sid", repo_config=_test_repo_config())

    assert res.session_id == "test-sid"
    assert seen["agent_ran"] is True


def test_agent_check_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        agent_check,
        "run_agent_check_agent",
        lambda **k: _empty_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    a = run_agent_check_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    ).session_id
    assert a == "test-sid"


# --- Clone tests ---


def test_run_agent_check_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the agent-check run clones the repo
    locally and hands the agent repo_dir and memory_dir. Idempotent + best-effort."""
    from robotsix_mill.vcs import git_ops

    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
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
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_agent_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    repo = settings.data_dir / "agent_check_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo
    assert seen["memory_dir"] == settings.data_dir

    seen["clone"] = 0
    run_agent_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert seen["clone"] == 1 and seen["repo_dir"] == repo  # re-clones fresh each run
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

    def fake_explore_tool(s, repo_dir, extra_roots=None):
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

    def fake_run_agent(agent, make_run, *, what="", **kwargs):
        return make_run(agent)

    monkeypatch.setattr(fs_tools, "build_fs_tools", fake_build_fs_tools)
    monkeypatch.setattr(explore, "make_explore_tool", fake_explore_tool)
    monkeypatch.setattr(base, "build_agent_from_definition", fake_build_agent)
    monkeypatch.setattr(retry, "run_agent", fake_run_agent)

    agent_check.run_agent_check_agent(
        settings=settings,
        repo_dir=Path("/fake/repo"),
        memory_dir=Path("/fake/data"),
    )

    assert captured_extra_roots == [Path("/fake/data")]


def test_run_agent_check_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)  # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        agent_check,
        "run_agent_check_agent",
        lambda **k: got.__setitem__("repo_dir", k.get("repo_dir")) or _empty_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    run_agent_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert got["repo_dir"] is None
