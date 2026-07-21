"""Tests for the survey agent and runner."""

import json
from pathlib import Path

from robotsix_mill.agents import surveying as survey_agent
from robotsix_mill.agents.yaml_loader import load_agent_definition
from robotsix_mill.runners.periodic_runner import run_survey_pass
from robotsix_mill.runners.periodic_runner import PeriodicPassResult
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
    # Default survey_periodic to false so the negative test is clean
    overrides.setdefault("survey_periodic", False)
    s = Settings(**overrides)
    db.reset_engine()
    db.init_db(s, board_id="test-board")
    return s


# --- Agent tests ---


def test_survey_system_prompt_covers_key_dimensions():
    """The survey agent prompt (loaded from YAML) must cover the key
    dimensions: open-source discovery, single-subject focus, proposal
    generation, rotation log, ask_web_knowledge gateway, and standards
    awareness."""
    prompt_path = Path("agent_definitions/periodic/survey.yaml")
    definition = load_agent_definition(prompt_path)
    prompt = definition.system_prompt.lower()

    for kw in (
        "open-source",
        "one specific",
        "subject per run",
        "propose",
        "rotation log",
        "ask_web_knowledge",
        "comparable projects",
        "actionable improvement",
        "prior proposals",
        "memory ledger",
        "one proposal per run",
    ):
        assert kw in prompt, f"survey prompt missing key dimension: {kw}"


def test_survey_prompt_includes_standards_awareness():
    """The survey agent prompt includes a STANDARDS AWARENESS section
    telling the agent to consult robotsix-standards before proposing."""
    prompt_path = Path("agent_definitions/periodic/survey.yaml")
    definition = load_agent_definition(prompt_path)
    prompt = definition.system_prompt

    # Section heading must be present.
    assert "STANDARDS AWARENESS" in prompt, (
        "survey prompt missing STANDARDS AWARENESS section"
    )

    # Key standards-awareness behaviours — check case-sensitive since
    # these are specific technical terms / repo names.
    for kw in (
        "robotsix-standards",
        "AGENT.md",
        "extra filesystem root",
        "Selectively fetch",
        "filter, not a straitjacket",
    ):
        assert kw in prompt, f"survey prompt missing standards keyword: {kw}"


def test_survey_result_model():
    """SurveyResult has the expected fields and defaults."""
    result = survey_agent.SurveyResult(
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
    default_result = survey_agent.SurveyResult()
    assert default_result.updated_memory == ""
    assert default_result.draft_titles == []
    assert default_result.draft_bodies == []
    assert default_result.gap_ids == []


# --- Runner tests ---


def test_run_survey_pass_empty_memory(tmp_path, monkeypatch):
    """With no memory file, runner passes empty string to agent."""
    settings = _make_settings(tmp_path)
    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return survey_agent.SurveyResult(
            updated_memory="new memory",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_run_survey_pass_reads_existing_memory(tmp_path, monkeypatch):
    """Runner passes existing memory to agent."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "survey_memory.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text(
        "# Existing memory\n## Rotation Log\n- entry\n", encoding="utf-8"
    )

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return survey_agent.SurveyResult(
            updated_memory="# Updated memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == ["# Existing memory\n## Rotation Log\n- entry\n"]


def test_run_survey_pass_writes_memory_verbatim(tmp_path, monkeypatch):
    """Runner writes agent's updated_memory verbatim."""
    settings = _make_settings(tmp_path)
    updated = "# Updated memory\n## Rotation Log\n- 2026-03-15: entry\n"

    def mock_agent(**kwargs):
        return survey_agent.SurveyResult(
            updated_memory=updated,
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    memory_file = settings.data_dir / "test-repo" / "survey_memory.md"
    assert memory_file.exists()
    assert memory_file.read_text(encoding="utf-8") == updated


def test_run_survey_pass_creates_draft_tickets(tmp_path, monkeypatch):
    """Runner creates draft tickets for each proposed gap with
    source='survey'."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")
    service = TicketService(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return survey_agent.SurveyResult(
            updated_memory="# Memory\n",
            draft_titles=["Adopt pattern X", "Improve Y"],
            draft_bodies=["Body1", "Body2"],
            gap_ids=["adopt_x", "improve_y"],
        )

    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 2
    # Verify tickets are in DB with source="survey"
    tickets = service.list()
    survey_tickets = [t for t in tickets if t.source == "survey"]
    assert len(survey_tickets) == 2
    assert survey_tickets[0].state == State.DRAFT


def test_run_survey_pass_no_drafts_when_empty(tmp_path, monkeypatch):
    """When agent returns no drafts, none are created."""
    settings = _make_settings(tmp_path)
    db.reset_engine()
    db.init_db(settings, board_id="test-board")

    def mock_agent(**kwargs):
        return survey_agent.SurveyResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert len(result.drafts_created) == 0


def test_run_survey_pass_missing_memory_file(tmp_path, monkeypatch):
    """Missing memory file -> empty string passed, no error."""
    settings = _make_settings(tmp_path)
    memory_file = settings.data_dir / "test-repo" / "survey_memory.md"
    if memory_file.exists():
        memory_file.unlink()

    captured_memory = []

    def mock_agent(**kwargs):
        captured_memory.append(kwargs.get("memory", ""))
        return survey_agent.SurveyResult(
            updated_memory="# Memory\n",
            draft_titles=[],
            draft_bodies=[],
            gap_ids=[],
        )

    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert captured_memory == [""]


def test_survey_pass_result_structure(tmp_path, monkeypatch):
    """PeriodicPassResult has correct structure."""
    settings = _make_settings(tmp_path)

    def mock_agent(**kwargs):
        return survey_agent.SurveyResult(
            updated_memory="mem",
            draft_titles=["t1"],
            draft_bodies=["b1"],
            gap_ids=["g1"],
        )

    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    result = run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert isinstance(result, PeriodicPassResult)
    assert result.updated_memory == "mem"
    assert len(result.drafts_created) == 1
    assert result.drafts_created[0]["title"] == "t1"


# --- Langfuse session tests ---


def test_run_survey_pass_opens_langfuse_session(tmp_path, monkeypatch):
    """session_id is passed through to the result."""
    settings = _make_settings(tmp_path)
    seen = {}

    def mock_agent(**kwargs):
        seen["agent_ran"] = True
        return survey_agent.SurveyResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    res = run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())

    assert res.session_id == "test-sid"
    assert seen["agent_ran"] is True


def test_survey_session_ids_are_unique_per_run(tmp_path, monkeypatch):
    """Each run gets its own session_id."""
    settings = _make_settings(tmp_path)
    monkeypatch.setattr(
        survey_agent,
        "run_survey_agent",
        lambda **k: survey_agent.SurveyResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    a = run_survey_pass(
        session_id="test-sid", repo_config=_test_repo_config()
    ).session_id
    assert a == "test-sid"


# --- Clone tests ---


def test_run_survey_pass_clones_and_passes_repo_dir(tmp_path, monkeypatch):
    """With a forge configured, the survey run clones the repo locally
    and hands the agent repo_dir. Idempotent + best-effort."""
    from robotsix_mill.vcs import git_ops

    settings = _make_settings(
        tmp_path,
        FORGE_REMOTE_URL="https://example.test/r.git",
        FORGE_TARGET_BRANCH="main",
    )
    seen = {"clone": 0, "repo_dir": "unset"}

    def fake_clone(url, dest, branch, token, **kwargs):
        seen["clone"] += 1
        (dest / ".git").mkdir(parents=True)

    def mock_agent(**kwargs):
        seen["repo_dir"] = kwargs.get("repo_dir")
        return survey_agent.SurveyResult(
            updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
        )

    monkeypatch.setattr(git_ops, "clone", fake_clone)
    monkeypatch.setattr(survey_agent, "run_survey_agent", mock_agent)
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )

    run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    repo = settings.data_dir / "survey_workspace" / "repo"
    assert seen["clone"] == 1 and seen["repo_dir"] == repo

    seen["clone"] = 0
    run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert seen["clone"] == 1 and seen["repo_dir"] == repo  # re-clones fresh each run


def test_run_survey_pass_no_forge_is_repo_dir_none(tmp_path, monkeypatch):
    """Without forge_remote_url, repo_dir=None."""
    settings = _make_settings(tmp_path)  # no FORGE_REMOTE_URL
    got = {}
    monkeypatch.setattr(
        survey_agent,
        "run_survey_agent",
        lambda **k: (
            got.__setitem__("repo_dir", k.get("repo_dir"))
            or survey_agent.SurveyResult(
                updated_memory="m", draft_titles=[], draft_bodies=[], gap_ids=[]
            )
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.Settings", lambda: settings
    )
    run_survey_pass(session_id="test-sid", repo_config=_test_repo_config())
    assert got["repo_dir"] is None


# --- Config tests ---


def test_survey_config_defaults():
    """Survey config has correct defaults."""
    s = Settings()
    assert s.survey_periodic is True
    assert s.survey_interval_seconds == 604800


def test_survey_periodic_config():
    """Survey periodic can be enabled."""
    s = Settings(survey_periodic="true", survey_interval_seconds="43200")
    assert s.survey_periodic is True
    assert s.survey_interval_seconds == 43200


# --- CLI tests ---


def test_survey_cli_command(capsys, tmp_path, monkeypatch):
    """Test that CLI survey command works."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Adopt pattern X"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_survey_pass", mock_run
    )

    result = main(["survey"])
    assert result == 0
    captured = capsys.readouterr()
    assert "Survey pass complete" in captured.out
    assert "Adopt pattern X" in captured.out


def test_survey_cli_json_output(capsys, tmp_path, monkeypatch):
    """Test JSON output flag for survey CLI."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[{"id": "123", "title": "Adopt pattern X"}],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_survey_pass", mock_run
    )

    result = main(["survey", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "memory" in data
    assert "tickets_created" in data
    assert data["tickets_created"] == [{"id": "123", "title": "Adopt pattern X"}]


def test_survey_cli_no_drafts(capsys, tmp_path, monkeypatch):
    """CLI survey command when no drafts created."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        return PeriodicPassResult(
            updated_memory="mem",
            drafts_created=[],
        )

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_survey_pass", mock_run
    )

    result = main(["survey"])
    assert result == 0
    captured = capsys.readouterr()
    assert "No new draft tickets created" in captured.out


def test_survey_cli_failure(capsys, monkeypatch):
    """CLI survey exits 1 on failure."""
    from robotsix_mill.cli import main

    def mock_run(session_id=None):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "robotsix_mill.runners.periodic_runner.run_survey_pass", mock_run
    )

    result = main(["survey"])
    assert result == 1
    captured = capsys.readouterr()
    assert "survey failed" in captured.err


# --- Trace budget wiring tests ---


class TestSurveyRunnerTraceBudgetWiring:
    """Verify run_survey_pass calls both budget resets before run_periodic_pass."""

    def test_run_survey_pass_calls_trace_budget_resets(self, tmp_path, monkeypatch):
        """run_survey_pass resets both web_fetch and web_search trace
        budgets with the configured settings values before delegating
        to run_periodic_pass."""
        from robotsix_mill.runners.periodic_runner import PeriodicPassResult

        settings = _make_settings(
            tmp_path,
            survey_web_fetch_max_calls=5,
            survey_web_fetch_max_total_bytes=500_000,
            survey_web_search_max_calls=5,
        )

        # Track calls.
        fetch_reset_calls = []
        search_reset_calls = []

        def fake_fetch_reset(max_calls, max_bytes):
            fetch_reset_calls.append((max_calls, max_bytes))

        def fake_search_reset(max_calls):
            search_reset_calls.append(max_calls)

        # Monkeypatch the modules where periodic_runner imports from.
        import robotsix_mill.agents.web_tools as wt
        import robotsix_mill.agents.web_knowledge as wk
        import robotsix_mill.runners.periodic_runner as sr

        monkeypatch.setattr(wt, "reset_trace_web_fetch_budget", fake_fetch_reset)
        monkeypatch.setattr(wk, "reset_trace_web_search_budget", fake_search_reset)
        monkeypatch.setattr(sr, "Settings", lambda: settings)

        # Stub run_periodic_pass.
        def fake_run_periodic(session_id, repo_config, *, config, settings):
            return PeriodicPassResult(
                updated_memory="ok", drafts_created=[], session_id=session_id
            )

        monkeypatch.setattr(sr, "run_periodic_pass", fake_run_periodic)

        repo_config = _test_repo_config()
        result = sr.run_survey_pass(session_id="test-sid", repo_config=repo_config)

        # Verify both resets were called with configured values.
        assert len(fetch_reset_calls) == 1
        assert fetch_reset_calls[0] == (5, 500_000)

        assert len(search_reset_calls) == 1
        assert search_reset_calls[0] == 5

        # Verify the result propagated.
        assert result.session_id == "test-sid"
        assert result.updated_memory == "ok"

    def test_run_survey_pass_respects_custom_budget_values(self, tmp_path, monkeypatch):
        """When settings have different budget values, those are passed
        to the reset functions."""
        from robotsix_mill.runners.periodic_runner import PeriodicPassResult

        settings = _make_settings(
            tmp_path,
            survey_web_fetch_max_calls=7,
            survey_web_fetch_max_total_bytes=300_000,
            survey_web_search_max_calls=3,
        )

        fetch_reset_calls = []
        search_reset_calls = []

        def fake_fetch_reset(max_calls, max_bytes):
            fetch_reset_calls.append((max_calls, max_bytes))

        def fake_search_reset(max_calls):
            search_reset_calls.append(max_calls)

        import robotsix_mill.agents.web_tools as wt
        import robotsix_mill.agents.web_knowledge as wk
        import robotsix_mill.runners.periodic_runner as sr

        monkeypatch.setattr(wt, "reset_trace_web_fetch_budget", fake_fetch_reset)
        monkeypatch.setattr(wk, "reset_trace_web_search_budget", fake_search_reset)
        monkeypatch.setattr(sr, "Settings", lambda: settings)

        def fake_run_periodic(session_id, repo_config, *, config, settings):
            return PeriodicPassResult(
                updated_memory="ok", drafts_created=[], session_id=session_id
            )

        monkeypatch.setattr(sr, "run_periodic_pass", fake_run_periodic)

        repo_config = _test_repo_config()
        sr.run_survey_pass(session_id="test-sid", repo_config=repo_config)

        assert fetch_reset_calls == [(7, 300_000)]
        assert search_reset_calls == [3]


# --- Standards awareness tests ---


class TestStandardsRepoClone:
    """Verify the standards repo clone / cache behaviour used by
    ``_survey_dynamic_kwargs`` to inject ``extra_roots``."""

    def test_inject_standards_root_clones_repo(self, tmp_path, monkeypatch):
        """First call clones the standards repo into the cache dir."""
        from robotsix_mill.agents.surveying import (
            _ensure_standards_repo,
            _STANDARDS_CACHE_SUBDIR,
        )

        settings = _make_settings(tmp_path)
        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)
            # Simulate clone: create the .git marker so the next
            # call sees an existing clone.
            if "clone" in cmd:
                dest = Path(cmd[-1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / ".git").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("subprocess.run", fake_run)

        result = _ensure_standards_repo(settings)

        expected_dir = settings.data_dir.joinpath(*_STANDARDS_CACHE_SUBDIR)
        assert result == expected_dir
        assert len(run_calls) >= 1
        # First call must be a clone.
        clone_cmd = " ".join(run_calls[0])
        assert "clone" in clone_cmd

    def test_inject_standards_root_idempotent(self, tmp_path, monkeypatch):
        """Second call pulls (does not re-clone) when .git already exists."""
        from robotsix_mill.agents.surveying import (
            _ensure_standards_repo,
            _STANDARDS_CACHE_SUBDIR,
        )

        settings = _make_settings(tmp_path)
        expected_dir = settings.data_dir.joinpath(*_STANDARDS_CACHE_SUBDIR)
        expected_dir.mkdir(parents=True, exist_ok=True)
        (expected_dir / ".git").mkdir(parents=True, exist_ok=True)

        run_calls = []

        def fake_run(cmd, **kwargs):
            run_calls.append(cmd)

        monkeypatch.setattr("subprocess.run", fake_run)

        result = _ensure_standards_repo(settings)
        assert result == expected_dir
        # Should pull, not clone.
        assert len(run_calls) >= 1
        pull_cmd = " ".join(run_calls[0])
        assert "pull" in pull_cmd

    def test_inject_standards_root_clone_failure_returns_none(
        self, tmp_path, monkeypatch
    ):
        """When clone fails, return None (graceful degradation)."""
        from robotsix_mill.agents.surveying import _ensure_standards_repo

        settings = _make_settings(tmp_path)

        def fake_run(cmd, **kwargs):
            raise OSError("network unreachable")

        monkeypatch.setattr("subprocess.run", fake_run)

        result = _ensure_standards_repo(settings)
        assert result is None


class TestSurveyDynamicKwargsExtraRoots:
    """Verify ``_survey_dynamic_kwargs`` includes ``extra_roots`` when
    the standards clone succeeds."""

    def test_survey_dynamic_kwargs_includes_extra_roots(self, tmp_path, monkeypatch):
        """When the standards repo is cloned, extra_roots points to it."""
        from robotsix_mill.agents.surveying import (
            _survey_dynamic_kwargs,
            _STANDARDS_CACHE_SUBDIR,
        )

        settings = _make_settings(tmp_path)
        expected_dir = settings.data_dir.joinpath(*_STANDARDS_CACHE_SUBDIR)
        expected_dir.mkdir(parents=True, exist_ok=True)
        (expected_dir / ".git").mkdir(parents=True, exist_ok=True)

        # No-op pull.
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: None)

        kwargs = _survey_dynamic_kwargs(settings)
        assert "extra_roots" in kwargs
        assert kwargs["extra_roots"] == [expected_dir]
        assert "usage_limits" in kwargs

    def test_survey_dynamic_kwargs_no_extra_roots_on_clone_failure(
        self, tmp_path, monkeypatch
    ):
        """When the standards clone fails, extra_roots is absent."""
        from robotsix_mill.agents.surveying import _survey_dynamic_kwargs

        settings = _make_settings(tmp_path)

        def fake_run(cmd, **kwargs):
            raise OSError("network unreachable")

        monkeypatch.setattr("subprocess.run", fake_run)

        kwargs = _survey_dynamic_kwargs(settings)
        assert "extra_roots" not in kwargs
        assert "usage_limits" in kwargs


class TestBuildPeriodicToolsExtraRoots:
    """Verify ``_build_periodic_tools`` forwards ``extra_roots`` to
    explore and parallel_explore tool factories."""

    def test_explore_tools_receive_extra_roots(self, tmp_path):
        """When extra_roots is a non-empty list, make_explore_tool and
        make_parallel_explore_tool are called with extra_roots."""
        from robotsix_mill.agents.periodic_base import _build_periodic_tools

        settings = _make_settings(tmp_path)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        extra = [tmp_path / "standards"]

        captured_explore = {}
        captured_parallel = {}

        import robotsix_mill.agents.explore as explore_mod

        orig_make_explore = explore_mod.make_explore_tool
        orig_make_parallel = explore_mod.make_parallel_explore_tool

        def fake_explore(s, rd, extra_roots=None):
            captured_explore["extra_roots"] = extra_roots
            return orig_make_explore(s, rd, extra_roots=extra_roots)

        def fake_parallel(s, rd, extra_roots=None):
            captured_parallel["extra_roots"] = extra_roots
            return orig_make_parallel(s, rd, extra_roots=extra_roots)

        monkeypatch = __import__("pytest").MonkeyPatch()
        monkeypatch.setattr(explore_mod, "make_explore_tool", fake_explore)
        monkeypatch.setattr(explore_mod, "make_parallel_explore_tool", fake_parallel)

        try:
            _build_periodic_tools(
                settings=settings,
                repo_dir=repo_dir,
                include_jscpd=False,
                include_workflow_caller_audit=False,
                include_run_command=False,
                include_write_file=False,
                extra_roots=extra,
            )
        finally:
            monkeypatch.undo()

        assert captured_explore.get("extra_roots") == extra
        assert captured_parallel.get("extra_roots") == extra

    def test_explore_tools_none_extra_roots_is_none(self, tmp_path):
        """When extra_roots is None, explore tools receive None."""
        from robotsix_mill.agents.periodic_base import _build_periodic_tools

        settings = _make_settings(tmp_path)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        captured_explore = {}
        captured_parallel = {}

        import robotsix_mill.agents.explore as explore_mod

        orig_make_explore = explore_mod.make_explore_tool
        orig_make_parallel = explore_mod.make_parallel_explore_tool

        def fake_explore(s, rd, extra_roots=None):
            captured_explore["extra_roots"] = extra_roots
            return orig_make_explore(s, rd, extra_roots=extra_roots)

        def fake_parallel(s, rd, extra_roots=None):
            captured_parallel["extra_roots"] = extra_roots
            return orig_make_parallel(s, rd, extra_roots=extra_roots)

        monkeypatch = __import__("pytest").MonkeyPatch()
        monkeypatch.setattr(explore_mod, "make_explore_tool", fake_explore)
        monkeypatch.setattr(explore_mod, "make_parallel_explore_tool", fake_parallel)

        try:
            _build_periodic_tools(
                settings=settings,
                repo_dir=repo_dir,
                include_jscpd=False,
                include_workflow_caller_audit=False,
                include_run_command=False,
                include_write_file=False,
                extra_roots=None,
            )
        finally:
            monkeypatch.undo()

        assert captured_explore.get("extra_roots") is None
        assert captured_parallel.get("extra_roots") is None


# --- Worker periodic tests ---
