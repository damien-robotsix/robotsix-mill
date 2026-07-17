"""Tests for the bc-check agent and runner."""

import json


from robotsix_mill.agents import bc_check as bc_check_agent
from robotsix_mill.runners.periodic_runner import run_bc_check_pass, BcCheckPassResult
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


# --- Agent tests ---


def test_bc_check_system_prompt_covers_all_six_patterns():
    """The bc-check agent prompt must cover all six detection patterns."""
    p = bc_check_agent.SYSTEM_PROMPT.lower()
    for kw in (
        "no-op compat",
        "legacy property",
        "alias assignments",
        "default-arg compat",
        "legacy shape fallbacks",
        "shim functions",
    ):
        assert kw in p, f"bc_check prompt missing pattern cue: {kw}"
    # Must exercise judgement, not just regex.
    assert "judgement" in p or "judgment" in p
    assert "not a static linter" in p or "static linter" in p
    # Must use the memory ledger to avoid re-filing.
    assert "memory" in p
    # Must use explore/read_file/list_dir tools.
    assert "explore" in p
    assert "read_file" in p
    assert "list_dir" in p


def test_bc_check_result_model():
    """BcCheckResult has the expected fields and defaults."""
    result = bc_check_agent.BcCheckResult(
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
    default_result = bc_check_agent.BcCheckResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


def test_bc_check_result_field_types():
    """BcCheckResult fields have correct types."""
    result = bc_check_agent.BcCheckResult(
        updated_memory="# BC Check Memory\n",
        draft_titles=["Remove no-op init()"],
        draft_bodies=["The init() in tracing.py is a no-op..."],
        gap_ids=["noop_init_tracing"],
    )
    assert isinstance(result.updated_memory, str)
    assert isinstance(result.draft_titles, list)
    assert isinstance(result.draft_bodies, list)
    assert isinstance(result.gap_ids, list)
    assert all(isinstance(t, str) for t in result.draft_titles)
    assert all(isinstance(b, str) for b in result.draft_bodies)
    assert all(isinstance(g, str) for g in result.gap_ids)


# --- Runner tests ---


def test_run_bc_check_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return bc_check_agent.BcCheckResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_bc_check_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "bc_check_memory.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("# Existing memory\n## Proposed\n- gap1\n", encoding="utf-8")

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return bc_check_agent.BcCheckResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == ["# Existing memory\n## Proposed\n- gap1\n"]


def test_run_bc_check_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Proposed\n- gap1\n"

    def mock_agent(**kwargs):
        return bc_check_agent.BcCheckResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    memory_file = settings.data_dir / "test-repo" / "bc_check_memory.md"
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_bc_check_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap with
    source='bc_check'."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return bc_check_agent.BcCheckResult(
            updated_memory="# Memory\n",
            draft_titles=["Remove no-op init()", "Drop legacy alias"],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["noop_init", "legacy_alias"],
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="bc_check"
    tickets = service.list()
    bc_tickets = [t for t in tickets if t.source == "bc_check"]
    assert len(bc_tickets) == 2
    assert bc_tickets[0].state == State.DRAFT


def test_run_bc_check_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return bc_check_agent.BcCheckResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 0


def test_run_bc_check_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "bc_check_memory.md"
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return bc_check_agent.BcCheckResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_bc_check_pass_result_structure(tmp_path, monkeypatch):
    """BcCheckPassResult has correct structure."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return bc_check_agent.BcCheckResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert isinstance(result, BcCheckPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


def test_run_bc_check_pass_skips_empty_title_or_body(tmp_path, monkeypatch):
    """Runner skips draft entries with empty title or body."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return bc_check_agent.BcCheckResult(
            updated_memory="mem",
            draft_titles=["Valid", "", "Also Valid"],
            draft_bodies=["Body", "Body2", ""],
            gap_ids=["g1", "g2", "g3"],
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 1  # only first has both title + body


# --- Config tests ---


def test_bc_check_config_defaults():
    """BC-check config has correct defaults."""
    s = Settings()
    assert s.bc_check_periodic is True
    assert s.bc_check_interval_seconds == 604800


def test_bc_check_periodic_config():
    """BC-check periodic can be enabled."""
    s = Settings(bc_check_periodic="true", bc_check_interval_seconds="43200")
    assert s.bc_check_periodic is True
    assert s.bc_check_interval_seconds == 43200


# --- CLI tests ---


def test_bc_check_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI bc-check command works."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return BcCheckPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Remove no-op init()"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_bc_check_pass", mock_run
    )

    result = main(["bc-check"])
    assert result == 0
    captured = capsys.readouterr()
    assert "BC-check pass complete" in captured.out
    assert "Remove no-op init()" in captured.out


def test_bc_check_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag for bc-check CLI."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return BcCheckPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Remove no-op init()"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_bc_check_pass", mock_run
    )

    result = main(["bc-check", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data
    assert data["tickets_created"] == [{"id": "123", "title": "Remove no-op init()"}]


def test_bc_check_cli_no_drafts(capsys, tmp_path, monkeypatch):
    """CLI bc-check command when no drafts created."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return BcCheckPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_bc_check_pass", mock_run
    )

    result = main(["bc-check"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No new draft tickets created" in captured.out


def test_bc_check_cli_failure(capsys, monkeypatch):
    """CLI bc-check exits 1 on failure."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_bc_check_pass", mock_run
    )

    result = main(["bc-check"])
    assert result == 1
    captured = capsys.readouterr()
    assert "bc-check failed" in captured.err


# --- Langfuse session tests ---


def test_run_bc_check_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """session_id is passed through to the result — tracing is now the
    poll loop's responsibility."""

    settings = _make_settings(tmp_path)
    seen = {}

    def mock_agent(**kwargs):
        seen["agent_ran"] = True
        return bc_check_agent.BcCheckResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    res = run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())

    assert res.session_id == "test-sid"
    assert seen["agent_ran"] is True


def test_bc_check_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        bc_check_agent,
        "run_bc_check_agent",
        lambda **k: bc_check_agent.BcCheckResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    a = run_bc_check_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    ).session_id
    assert a == "test-sid"


# --- Clone tests ---


def test_run_bc_check_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the bc-check run clones the repo locally
    and hands the agent repo_dir."""
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
        return bc_check_agent.BcCheckResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(bc_check_agent, "run_bc_check_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    repo = settings.data_dir / "bc_check_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo

    seen["clone"] = 0
    run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert seen["clone"] == 1 and seen["repo_dir"] == repo  # re-clones fresh each run


def test_run_bc_check_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    settings = _make_settings(tmp_path)  # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        bc_check_agent,
        "run_bc_check_agent",
        lambda **k: (
            got.__setitem__("repo_dir", k.get("repo_dir"))
            or bc_check_agent.BcCheckResult(
                updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
            )
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    run_bc_check_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert got["repo_dir"] is None


# --- Worker periodic tests ---
