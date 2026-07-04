"""Tests for the maintenance agent module."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents.maintenance import (
    MaintenanceResult,
    _validate_command,
    make_clone_repo_tool,
    make_create_repo_tool,
    make_fork_repo_tool,
    run_maintenance_agent,
)
from robotsix_mill.agents.tool_registry import ToolRegistry
from robotsix_mill.config import Secrets, Settings, _reset_secrets


# ── helpers ──────────────────────────────────────────────────────────


def _settings(tmp_path, **env):
    """Build a Settings for testing with a fake OpenRouter key."""
    env.setdefault("data_dir", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        _reset_secrets()
        import robotsix_mill.config as _cfg

        _cfg._secrets = Secrets(openrouter_api_key=key)
    return Settings(**env)


@pytest.fixture(autouse=True)
def _clear_registry():
    """Each test starts with a clean ToolRegistry."""
    ToolRegistry._tools.clear()
    yield
    ToolRegistry._tools.clear()


# ── MaintenanceResult model ──────────────────────────────────────────


class TestMaintenanceResult:
    def test_defaults(self):
        """success is required; note defaults to None."""
        r = MaintenanceResult(success=False)
        assert r.success is False
        assert r.note is None

    def test_explicit(self):
        """Can be constructed with success=True and a note."""
        r = MaintenanceResult(success=True, note="repo created")
        assert r.success is True
        assert r.note == "repo created"

    def test_stage_contract(self):
        """Has the .success and .note attributes expected by
        MaintenanceStage.run()."""
        r = MaintenanceResult(success=False, note="fork failed")
        assert hasattr(r, "success")
        assert hasattr(r, "note")
        # The stage reads .success and .note directly
        assert r.success is False
        assert r.note == "fork failed"


class TestMaintenanceResultRedirect:
    """Tests for the redirect_to field on MaintenanceResult."""

    def test_redirect_to_defaults_to_none(self):
        """When not provided, redirect_to is None."""
        r = MaintenanceResult(success=True)
        assert r.redirect_to is None

    def test_redirect_to_parses_ready_string(self):
        """The raw string 'ready' is coerced to State.READY."""
        from robotsix_mill.core.states import State

        r = MaintenanceResult(**{"success": True, "redirect_to": "ready"})
        assert r.redirect_to == State.READY

    def test_redirect_to_parses_draft_string(self):
        """The raw string 'draft' is coerced to State.DRAFT."""
        from robotsix_mill.core.states import State

        r = MaintenanceResult(**{"success": True, "redirect_to": "draft"})
        assert r.redirect_to == State.DRAFT

    def test_redirect_to_accepts_state_enum(self):
        """Passing a State enum directly works."""
        from robotsix_mill.core.states import State

        r = MaintenanceResult(success=True, redirect_to=State.READY)
        assert r.redirect_to == State.READY

    def test_redirect_to_accepts_none_explicitly(self):
        """Explicit None is accepted."""
        r = MaintenanceResult(success=True, redirect_to=None)
        assert r.redirect_to is None

    def test_redirect_to_rejects_invalid_state(self):
        """A non-ready/non-draft string raises ValidationError."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MaintenanceResult(success=True, redirect_to="implement")

    def test_redirect_to_rejects_arbitrary_string(self):
        """A non-state string like 'bogus' raises ValidationError."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MaintenanceResult(success=True, redirect_to="bogus")

    def test_redirect_to_rejects_done_state_enum(self):
        """Passing State.DONE directly raises ValidationError."""
        import pytest
        from pydantic import ValidationError
        from robotsix_mill.core.states import State

        with pytest.raises(ValidationError):
            MaintenanceResult(success=False, redirect_to=State.DONE)


# ── Stub tools ───────────────────────────────────────────────────────


class TestStubTools:
    def test_create_repo_tool_returns_error_when_no_forge(self, tmp_path):
        """Without a configured forge, create_repo returns an error string."""
        s = _settings(tmp_path)
        # build a minimal StageContext mock
        ctx = MagicMock()
        ctx.settings = s
        fn = make_create_repo_tool(s, ctx, ticket_description="Test draft")
        result = fn(name="my-repo", owner="owner", private=False, description="")
        assert "create_repo:" in result

    def test_fork_repo_tool_returns_error_when_no_forge(self, tmp_path):
        """Without a configured forge, fork_repo returns an error string."""
        s = _settings(tmp_path)
        fn = make_fork_repo_tool(s)
        result = fn(source_owner="alice", source_repo="my-repo")
        assert "fork_repo:" in result

    def test_stubs_accept_kwargs(self, tmp_path):
        """Stubs accept all documented parameters without error."""
        s = _settings(tmp_path)
        ctx = MagicMock()
        ctx.settings = s

        # create_repo with all params including language
        r1 = make_create_repo_tool(s, ctx, "draft")(
            name="x", owner="org", private=True, description="desc", language="python"
        )
        assert "create_repo:" in r1

        # fork_repo with target_namespace
        r2 = make_fork_repo_tool(s)(
            source_owner="a", source_repo="b", target_namespace="org"
        )
        assert "fork_repo:" in r2

    def test_create_repo_tool_success(self, tmp_path, monkeypatch):
        """With a mocked forge + scaffold, create_repo returns success JSON."""
        import json

        from robotsix_mill.forge.base import RepoInfo
        from robotsix_mill.core.states import State
        from robotsix_mill.stages.base import Outcome

        s = _settings(tmp_path)
        ctx = MagicMock()
        ctx.settings = s

        # Mock get_forge to return a fake forge (lazy-imported inside the closure)
        fake_forge = MagicMock()
        fake_forge.create_repo.return_value = RepoInfo(
            id=42,
            name="my-repo",
            clone_url="https://github.com/owner/my-repo.git",
            html_url="https://github.com/owner/my-repo",
        )
        monkeypatch.setattr(
            "robotsix_mill.forge.get_forge",
            lambda settings, repo_config=None: fake_forge,
        )

        # Mock run_repo_scaffold to return DONE
        def _fake_scaffold(settings, forge, ctx_, params, ticket_description):
            return Outcome(State.DONE, note="created + registered my-repo")

        monkeypatch.setattr(
            "robotsix_mill.repo_scaffold.run_repo_scaffold",
            _fake_scaffold,
        )

        fn = make_create_repo_tool(s, ctx, ticket_description="Create my-repo")
        result = fn(
            name="my-repo",
            owner="owner",
            private=False,
            description="A new repo",
            language="python",
        )
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["id"] == 42
        assert parsed["name"] == "my-repo"
        assert "github.com" in parsed["clone_url"]
        assert "github.com" in parsed["html_url"]
        assert "my-repo" in parsed["note"]


# ── Tool registry entries ────────────────────────────────────────────


class TestToolRegistryEntries:
    def test_maintenance_tools_registered(self, tmp_path):
        """After importing maintenance.py and calling the factories,
        ToolRegistry includes the maintenance tools."""
        s = _settings(tmp_path)
        ctx = MagicMock()
        ctx.settings = s
        # The factories register on call, so we must call them.
        make_create_repo_tool(s, ctx, ticket_description="draft")
        make_fork_repo_tool(s)
        make_clone_repo_tool(s, tmp_path)

        # Also register post_findings by calling its factory
        from robotsix_mill.agents.maintenance import make_post_findings_tool

        make_post_findings_tool(s, agent_name="test")

        # Register fs tools and explore tools
        from robotsix_mill.agents.fs_tools import build_fs_tools
        from robotsix_mill.agents.explore import make_explore_tool

        build_fs_tools(tmp_path, s)
        make_explore_tool(s, tmp_path)

        names = {t.name for t in ToolRegistry.list_tools()}
        assert "create_repo" in names
        assert "fork_repo" in names
        assert "clone_repo" in names
        assert "post_findings" in names
        assert "explore" in names
        assert "read_file" in names
        assert "list_dir" in names
        assert "run_command" in names


# ── Command allowlist ────────────────────────────────────────────────


class TestCommandAllowlist:
    """Tests for _validate_command and the run_command allowlist wrapper."""

    def test_allows_safe_commands(self):
        """Safe commands pass validation and return None."""
        assert _validate_command("git log") is None
        assert _validate_command("grep -r 'TODO'") is None
        assert _validate_command("ls -la") is None
        assert _validate_command("find . -name '*.py'") is None
        assert _validate_command("cat README.md") is None
        assert _validate_command("head -20 file.txt") is None
        assert _validate_command("tail -5 file.txt") is None
        assert _validate_command("wc -l *.py") is None
        assert _validate_command("sort file.txt") is None
        assert _validate_command("uniq -c") is None
        assert _validate_command("diff a.txt b.txt") is None
        assert _validate_command("sed 's/foo/bar/' file") is None
        assert _validate_command("awk '{print $1}'") is None
        assert _validate_command("cut -d: -f1") is None
        assert _validate_command("tr 'a-z' 'A-Z'") is None
        assert _validate_command("xargs echo") is None
        assert _validate_command("echo hello") is None
        assert _validate_command("dirname /a/b/c") is None
        assert _validate_command("basename /a/b/c.txt") is None
        assert _validate_command("realpath .") is None
        assert _validate_command("readlink -f .") is None
        assert _validate_command("stat file.txt") is None
        assert _validate_command("file README.md") is None
        assert _validate_command("du -sh .") is None
        assert _validate_command("tree -L 2") is None

    def test_rejects_destructive_commands(self):
        """Destructive / write-capable commands are rejected."""
        assert _validate_command("rm -rf /") is not None
        assert _validate_command("make") is not None
        assert _validate_command("curl http://example.com") is not None
        assert _validate_command("chmod 777 file") is not None
        assert _validate_command("pip install x") is not None
        assert _validate_command("python -c '...'") is not None
        assert _validate_command("npm install") is not None
        assert _validate_command("wget http://x") is not None
        assert _validate_command("mv a b") is not None
        assert _validate_command("cp a b") is not None
        assert _validate_command("touch file") is not None
        assert _validate_command("mkdir dir") is not None

    def test_allows_compound_commands(self):
        """Compound commands with cd prefixes are allowed when every
        segment's executable is in the allowlist."""
        assert _validate_command("cd subdir && git log --oneline -5") is None
        assert _validate_command("cd /some/path || ls") is None
        assert _validate_command("cd a && cd b && grep -r pattern") is None
        assert _validate_command("ls -la | head -20") is None
        assert _validate_command("grep -r foo . | sort | uniq -c") is None
        assert _validate_command("find . -name '*.py' | xargs grep TODO") is None

    def test_rejects_compound_with_destructive_segment(self):
        """A compound command where any segment is destructive is rejected."""
        assert _validate_command("cd subdir && rm file") is not None
        assert _validate_command("ls | curl http://x") is not None
        assert _validate_command("grep foo . ; make install") is not None

    def test_allows_pure_cd(self):
        """A pure cd command (no following command) is allowed."""
        assert _validate_command("cd subdir") is None
        assert _validate_command("cd /some/path") is None

    def test_rejects_command_with_rejection_message(self):
        """The rejection message names the rejected command and lists
        allowed commands."""
        err = _validate_command("rm -rf /")
        assert err is not None
        assert "rm" in err
        assert "git" in err  # allowed commands are listed

    def test_pipe_inside_single_quotes_not_treated_as_separator(self):
        """A ``|`` inside single quotes is part of a grep regex, not a pipe."""
        assert _validate_command("grep 'error|warning' file.txt") is None
        assert _validate_command("grep -E 'foo|bar|baz' *.py") is None

    def test_pipe_inside_double_quotes_not_treated_as_separator(self):
        """A ``|`` inside double quotes is part of a grep regex, not a pipe."""
        assert _validate_command('grep "error|warning" file.txt') is None
        assert _validate_command('grep -E "foo|bar|baz" *.py') is None

    def test_real_pipe_between_safe_commands_still_works(self):
        """Real (unquoted) pipes between safe commands are still allowed."""
        assert _validate_command("grep error | grep warning") is None
        assert _validate_command("ls -la | wc -l") is None

    def test_pipe_inside_quotes_with_real_pipe(self):
        """A quoted ``|`` in one segment plus a real pipe in another."""
        assert _validate_command("grep 'a|b' file.txt | sort | uniq -c") is None

    def test_semicolon_inside_quotes_not_split(self):
        """A ``;`` inside quotes is not treated as a command separator."""
        assert _validate_command("grep 'foo;bar' file.txt") is None

    def test_unmatched_quote_does_not_crash(self):
        """An unmatched single quote does not raise an exception."""
        result = _validate_command("grep 'unmatched")
        # The grep binary itself is allowed; the command is malformed
        # but validation should not crash.
        assert result is None

        result = _validate_command('grep "unmatched')
        assert result is None


# ── YAML definition ──────────────────────────────────────────────────


class TestYamlDefinition:
    def test_definition_loads(self):
        """The maintenance.yaml file parses without validation errors."""
        from robotsix_mill.agents.yaml_loader import load_agent_definition

        repo_root = Path(__file__).parent.parent.parent
        path = repo_root / "agent_definitions" / "maintenance.yaml"
        definition = load_agent_definition(path)

        assert definition.name == "maintenance"
        assert definition.category == "pipeline"
        assert definition.module == "maintenance"
        assert definition.reply_to_thread is False
        assert definition.close_thread is False
        assert definition.ask_user is True
        assert definition.report_issue is True
        assert definition.retries == 2
        assert definition.output_type == "MaintenanceResult"
        assert "clone_repo" in definition.tools
        assert "explore" in definition.tools
        assert "read_file" in definition.tools
        assert "list_dir" in definition.tools
        assert "board-report" in definition.skills
        assert "ask_user_guardrails" in definition.skills


# ── Agent construction (integration) ──────────────────────────────────


class TestAgentConstruction:
    def test_run_maintenance_agent_returns_result(self, tmp_path, monkeypatch):
        """run_maintenance_agent calls through without exception and
        returns a MaintenanceResult with .success and .note."""
        s = _settings(tmp_path)

        # Mock pydantic_ai.Agent so we don't need a real API key
        cap: dict = {}

        class FakeModel:
            def __init__(self, name, **kw):
                pass

        class FakeAgent:
            def __init__(self, **kw):
                cap["kw"] = kw

            def run_sync(self, prompt, *, usage_limits=None, **kw):
                return type(
                    "R", (), {"output": MaintenanceResult(success=True, note="test ok")}
                )()

        monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
        monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
        monkeypatch.setattr(
            "robotsix_mill.agents.base.new_deepseek_model",
            lambda model_name, level: (FakeModel(model_name), object()),
        )

        # Build mocks for ticket + ctx
        ticket = MagicMock()
        ticket.id = "test-ticket-1"
        ticket.board_id = "test-board"
        ticket.title = "Test ticket"

        ctx = MagicMock()
        ctx.settings = s
        ctx.repo_config = None

        # Mock workspace
        ws_mock = MagicMock()
        ws_mock.dir = tmp_path / "nonexistent"
        ws_mock.repo_dir = tmp_path / "nonexistent" / "repo"
        ws_mock.read_description.return_value = "Test draft"
        ctx.service.workspace.return_value = ws_mock

        result = run_maintenance_agent(ticket, ctx)

        assert isinstance(result, MaintenanceResult)
        assert result.success is True
        assert result.note == "test ok"

        # Verify the agent was built with the expected tools
        built_tools = cap["kw"].get("tools", [])
        tool_names = {getattr(t, "__name__", None) or str(t) for t in built_tools}
        # Expected tools should be present
        assert "create_repo" in tool_names
        assert "fork_repo" in tool_names
        assert "clone_repo" in tool_names
        assert "post_findings" in tool_names
        assert "explore" in tool_names
        assert "read_file" in tool_names
        assert "list_dir" in tool_names
        assert "run_command" in tool_names
        # Write tools should NOT be present (read-only enforcement)
        assert "write_file" not in tool_names
        assert "edit_file" not in tool_names
        assert "delete_file" not in tool_names

    def test_clone_dir_passed_as_extra_root(self, tmp_path, monkeypatch):
        """run_maintenance_agent forwards the clone dir (``<tmpdir>/repo``)
        as an extra root to all three investigation-tool factories."""
        s = _settings(tmp_path)

        captured: dict = {}

        def _dummy_fs_tool(name):
            def _fn(*a, **k):
                pass

            _fn.__name__ = name
            return _fn

        def fake_build_fs_tools(root, settings, *, pre_seeded=None, extra_roots=None):
            captured["fs"] = extra_roots
            return [_dummy_fs_tool(n) for n in ("read_file", "list_dir", "run_command")]

        def fake_make_explore_tool(settings, repo_dir, extra_roots=None):
            captured["explore"] = extra_roots
            return _dummy_fs_tool("explore")

        def fake_make_parallel_explore_tool(settings, repo_dir, extra_roots=None):
            captured["parallel_explore"] = extra_roots
            return _dummy_fs_tool("parallel_explore")

        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools",
            fake_build_fs_tools,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool",
            fake_make_explore_tool,
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_parallel_explore_tool",
            fake_make_parallel_explore_tool,
        )

        class FakeModel:
            def __init__(self, name, **kw):
                pass

        class FakeAgent:
            def __init__(self, **kw):
                pass

            def run_sync(self, prompt, *, usage_limits=None, **kw):
                return type(
                    "R", (), {"output": MaintenanceResult(success=True, note="ok")}
                )()

        monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
        monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
        monkeypatch.setattr(
            "robotsix_mill.agents.base.new_deepseek_model",
            lambda model_name, level: (FakeModel(model_name), object()),
        )

        ticket = MagicMock()
        ticket.id = "test-ticket-1"
        ticket.board_id = "test-board"
        ticket.title = "Test ticket"

        ctx = MagicMock()
        ctx.settings = s
        ctx.repo_config = None

        ws_mock = MagicMock()
        ws_mock.dir = tmp_path / "nonexistent"
        ws_mock.repo_dir = tmp_path / "nonexistent" / "repo"
        ws_mock.read_description.return_value = "Test draft"
        ctx.service.workspace.return_value = ws_mock

        run_maintenance_agent(ticket, ctx)

        for key in ("fs", "explore", "parallel_explore"):
            extra = captured[key]
            assert isinstance(extra, list) and extra, f"{key} extra_roots empty"
            assert extra[0].name == "repo"

    def test_run_maintenance_agent_stage_contract(self, tmp_path, monkeypatch):
        """The returned object satisfies the MaintenanceStage.run()
        contract: it has .success and .note attributes."""
        s = _settings(tmp_path)

        class FakeModel:
            def __init__(self, name, **kw):
                pass

        class FakeAgent:
            def __init__(self, **kw):
                pass

            def run_sync(self, prompt, *, usage_limits=None, **kw):
                return type(
                    "R",
                    (),
                    {
                        "output": MaintenanceResult(
                            success=False, note="fork failed: rate limited"
                        )
                    },
                )()

        monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
        monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
        monkeypatch.setattr(
            "robotsix_mill.agents.base.new_deepseek_model",
            lambda model_name, level: (FakeModel(model_name), object()),
        )

        ticket = MagicMock()
        ticket.id = "t2"
        ticket.board_id = "b2"
        ticket.title = "Test ticket"

        ctx = MagicMock()
        ctx.settings = s
        ctx.repo_config = None
        ws_mock = MagicMock()
        ws_mock.dir = tmp_path / "nonexistent"
        ws_mock.repo_dir = tmp_path / "nonexistent" / "repo"
        ws_mock.read_description.return_value = "Test draft"
        ctx.service.workspace.return_value = ws_mock

        result = run_maintenance_agent(ticket, ctx)

        # The existing stage test reads .success and .note directly
        assert result.success is False
        assert "rate limited" in result.note


# ── Existing stage test compatibility ─────────────────────────────────


class TestStageIntegration:
    """Verify the real module satisfies the same contract the
    existing stage test (tests/stages/test_maintenance.py) expects."""

    def test_module_is_importable(self):
        """The real ``robotsix_mill.agents.maintenance`` module exists
        and exports ``run_maintenance_agent``, so the stage's lazy
        import succeeds."""
        from robotsix_mill.agents import maintenance as mod

        assert hasattr(mod, "run_maintenance_agent")
        assert callable(mod.run_maintenance_agent)

    def test_remove_injected_mock(self):
        """Sanity check: the REAL maintenance module is importable and is not
        a leftover mock (the stage test injects a mock under a try/finally;
        this confirms no leak). Imports the module explicitly so the test is
        order- and xdist-worker-independent — it must NOT assume some prior
        test in the same worker already imported it (under ``--dist loadscope``
        the worker running this class may not have)."""
        import robotsix_mill.agents.maintenance as mod

        # The explicit import guarantees presence in sys.modules; the real
        # module (not a bare mock) exposes MaintenanceResult.
        assert "robotsix_mill.agents.maintenance" in sys.modules
        assert hasattr(mod, "MaintenanceResult")


# ── Repo context, request cap, and board wiring (regressions from the
#    blocked-tickets investigation: guessed clone URLs, implicit
#    pydantic-ai request cap of 50, report_issue "board_id is required") ──


class TestAgentRunWiring:
    def _fake_model_stack(self, monkeypatch, cap):
        class FakeModel:
            def __init__(self, name, **kw):
                pass

        class FakeAgent:
            def __init__(self, **kw):
                cap["agent_kw"] = kw

            def run_sync(self, prompt, *, usage_limits=None, **kw):
                cap["prompt"] = prompt
                cap["usage_limits"] = usage_limits
                return type(
                    "R", (), {"output": MaintenanceResult(success=True, note="ok")}
                )()

        monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
        monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
        monkeypatch.setattr(
            "robotsix_mill.agents.base.new_deepseek_model",
            lambda model_name, level: (FakeModel(model_name), object()),
        )

    def _ticket_ctx(self, tmp_path, s):
        ticket = MagicMock()
        ticket.id = "t-wiring"
        ticket.board_id = "board-x"
        ticket.title = "Wiring test"
        ctx = MagicMock()
        ctx.settings = s
        ctx.repo_config = None
        ws_mock = MagicMock()
        ws_mock.dir = tmp_path / "nonexistent"
        ws_mock.repo_dir = tmp_path / "nonexistent" / "repo"
        ws_mock.read_description.return_value = "Test draft"
        ctx.service.workspace.return_value = ws_mock
        return ticket, ctx

    def test_prompt_carries_board_and_real_clone_url(self, tmp_path, monkeypatch):
        """The agent must never have to GUESS the remote (live failure:
        guessed robotsix/mill.git, watched 3 clones die, misdiagnosed
        'network unavailable')."""
        s = _settings(tmp_path)
        cap: dict = {}
        self._fake_model_stack(monkeypatch, cap)
        monkeypatch.setattr(
            "robotsix_mill.forge.auth._resolve_remote_url",
            lambda settings, repo_config: "https://github.com/o/real-repo.git",
        )
        ticket, ctx = self._ticket_ctx(tmp_path, s)
        run_maintenance_agent(ticket, ctx)
        assert "# Board\nboard-x" in cap["prompt"]
        assert "https://github.com/o/real-repo.git" in cap["prompt"]
        assert "do not guess" in cap["prompt"]

    def test_prompt_without_resolvable_url_still_carries_board(
        self, tmp_path, monkeypatch
    ):
        s = _settings(tmp_path)
        cap: dict = {}
        self._fake_model_stack(monkeypatch, cap)
        monkeypatch.setattr(
            "robotsix_mill.forge.auth._resolve_remote_url",
            lambda settings, repo_config: None,
        )
        ticket, ctx = self._ticket_ctx(tmp_path, s)
        run_maintenance_agent(ticket, ctx)
        assert "# Board\nboard-x" in cap["prompt"]
        assert "clone URL" not in cap["prompt"]

    def test_explicit_request_limit_from_settings(self, tmp_path, monkeypatch):
        """The run carries an explicit UsageLimits bound to
        settings.maintenance_request_limit — not the implicit pydantic-ai
        default of 50 that produced opaque UsageLimitExceeded blocks."""
        s = _settings(tmp_path, maintenance_request_limit=7)
        cap: dict = {}
        self._fake_model_stack(monkeypatch, cap)
        ticket, ctx = self._ticket_ctx(tmp_path, s)
        run_maintenance_agent(ticket, ctx)
        assert cap["usage_limits"] is not None
        assert cap["usage_limits"].request_limit == 7

    def test_board_id_forwarded_to_agent_build(self, tmp_path, monkeypatch):
        """board_id reaches build_agent_from_definition so the report_issue
        tool binds to the ticket's board (post-migration regression:
        'db._db_path: board_id is required')."""
        from robotsix_mill.agents import base as agents_base

        s = _settings(tmp_path)
        cap: dict = {}
        self._fake_model_stack(monkeypatch, cap)
        seen: dict = {}
        real_build = agents_base.build_agent_from_definition

        def _spy(settings, definition, **kw):
            seen.update(kw)
            return real_build(settings, definition, **kw)

        monkeypatch.setattr(agents_base, "build_agent_from_definition", _spy)
        ticket, ctx = self._ticket_ctx(tmp_path, s)
        run_maintenance_agent(ticket, ctx)
        assert seen.get("board_id") == "board-x"


# ── Meta multi-repo workspace wiring (regression: ticket 6e68 — a
#    meta-board PyPI-audit blocked because the single-repo maintenance
#    model pointed every fs tool at the non-existent ``<ws>/repo`` dir
#    instead of the pre-cloned ``<ws>/repos/<id>`` clones) ──────────────


class TestMetaMultiRepoWorkspace:
    def _fake_model_stack(self, monkeypatch, cap):
        class FakeModel:
            def __init__(self, name, **kw):
                pass

        class FakeAgent:
            def __init__(self, **kw):
                pass

            def run_sync(self, prompt, *, usage_limits=None, **kw):
                cap["prompt"] = prompt
                return type(
                    "R", (), {"output": MaintenanceResult(success=True, note="ok")}
                )()

        monkeypatch.setattr(pydantic_ai, "Agent", FakeAgent)
        monkeypatch.setattr(orp, "OpenRouterProvider", lambda **kw: object())
        monkeypatch.setattr(
            "robotsix_mill.agents.base.new_deepseek_model",
            lambda model_name, level: (FakeModel(model_name), object()),
        )

    def _capture_fs_tools(self, monkeypatch, captured):
        def _dummy(name):
            def _fn(*a, **k):
                pass

            _fn.__name__ = name
            return _fn

        def fake_build_fs_tools(root, settings, *, pre_seeded=None, extra_roots=None):
            captured["fs_root"] = root
            captured["fs"] = extra_roots
            return [_dummy(n) for n in ("read_file", "list_dir", "run_command")]

        def fake_make_explore_tool(settings, repo_dir, extra_roots=None):
            captured["explore_root"] = repo_dir
            captured["explore"] = extra_roots
            return _dummy("explore")

        def fake_make_parallel_explore_tool(settings, repo_dir, extra_roots=None):
            captured["parallel_explore"] = extra_roots
            return _dummy("parallel_explore")

        monkeypatch.setattr(
            "robotsix_mill.agents.fs_tools.build_fs_tools", fake_build_fs_tools
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_explore_tool", fake_make_explore_tool
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.explore.make_parallel_explore_tool",
            fake_make_parallel_explore_tool,
        )

    def _meta_ticket_ctx(self, tmp_path, s):
        ticket = MagicMock()
        ticket.id = "t-meta"
        ticket.board_id = "meta"
        ticket.title = "Audit repos"
        ctx = MagicMock()
        ctx.settings = s
        ctx.repo_config = None
        ws_mock = MagicMock()
        ws_mock.dir = tmp_path / "ws"
        ws_mock.repo_dir = tmp_path / "ws" / "repo"
        ws_mock.read_description.return_value = "Audit all repos for PyPI"
        ctx.service.workspace.return_value = ws_mock
        return ticket, ctx

    def test_meta_workspace_roots_flow_into_tools_and_prompt(
        self, tmp_path, monkeypatch
    ):
        """For a meta ticket, the investigation_root is the clones' shared
        PARENT (so sandboxed run_command/explore see every repo as a subdir),
        the clones flow through as extra_roots on every investigation tool,
        and the prompt lists the pre-cloned repos."""
        s = _settings(tmp_path)
        cap: dict = {}
        captured: dict = {}
        self._fake_model_stack(monkeypatch, cap)
        self._capture_fs_tools(monkeypatch, captured)

        primary = tmp_path / "ws" / "repos" / "robotsix-mill"
        second = tmp_path / "ws" / "repos" / "robotsix-modules"
        repos_parent = primary.parent  # tmp_path/ws/repos

        def fake_build(ctx_, ticket_, ws_, spec, *, author):
            assert author == "maintenance"
            return primary, [primary, second], None

        monkeypatch.setattr(
            "robotsix_mill.meta.workspace.build_triaged_meta_workspace", fake_build
        )

        ticket, ctx = self._meta_ticket_ctx(tmp_path, s)
        result = run_maintenance_agent(ticket, ctx)

        assert result.success is True
        # investigation_root is the clones' PARENT (so the sandbox mounts all
        # repos), NOT a single clone and NOT ws.repo_dir
        assert captured["fs_root"] == repos_parent
        assert captured["explore_root"] == repos_parent
        # both meta clones reach every investigation tool's extra_roots
        for key in ("fs", "explore", "parallel_explore"):
            names = {p.name for p in captured[key]}
            assert {"robotsix-mill", "robotsix-modules"} <= names, key
        # prompt steers the agent to the pre-cloned repos, away from clone_repo
        assert "Pre-cloned repositories" in cap["prompt"]
        assert "robotsix-modules" in cap["prompt"]

    def test_meta_workspace_build_failure_blocks(self, tmp_path, monkeypatch):
        """When build_triaged_meta_workspace returns a blocking Outcome,
        run_maintenance_agent surfaces it as MaintenanceResult(success=False)
        without running the agent."""
        from robotsix_mill.core.states import State
        from robotsix_mill.stages.base import Outcome

        s = _settings(tmp_path)
        cap: dict = {}
        self._fake_model_stack(monkeypatch, cap)

        def fake_build(ctx_, ticket_, ws_, spec, *, author):
            return None, None, Outcome(State.BLOCKED, "meta repo-triage failed")

        monkeypatch.setattr(
            "robotsix_mill.meta.workspace.build_triaged_meta_workspace", fake_build
        )

        ticket, ctx = self._meta_ticket_ctx(tmp_path, s)
        result = run_maintenance_agent(ticket, ctx)

        assert result.success is False
        assert "meta repo-triage failed" in result.note
        # the agent never ran (no prompt captured)
        assert "prompt" not in cap


class TestCloneDirPrecreation:
    """Regression: the maintenance clone target must exist before the
    agent runs (live case: 81f1 Fatal-blocked with
    ``FileNotFoundError: /tmp/maintenance_*/repo`` when a tool touched
    ``clone_dir`` before ``clone_repo`` populated it)."""

    def test_clone_dir_exists_when_agent_runs(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from robotsix_mill.core import db
        from robotsix_mill.agents import base
        from robotsix_mill.agents import retry as retry_mod
        from robotsix_mill.config import RepoConfig
        from robotsix_mill.core.service import TicketService
        from robotsix_mill.stages import StageContext

        s = _settings(tmp_path)
        db.reset_engine()
        db.init_db(s, board_id="test-board")
        service = TicketService(s, board_id="test-board")
        repo_config = RepoConfig(
            repo_id="test-repo",
            langfuse_project_name="test-project",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        ticket = service.create("Bump lockfile", "investigate the failure")
        # The singular investigation root must exist (fs tools' root).
        service.workspace(ticket).repo_dir.mkdir(parents=True, exist_ok=True)
        ctx = StageContext(settings=s, service=service, repo_config=repo_config)

        # Capture the tempdir that clone_repo would clone into. Patch the
        # function's OWN globals (not the imported ``maintenance`` module):
        # tests/stages/test_maintenance.py swaps a mock module into
        # ``sys.modules`` and pops it, so a later ``import maintenance`` can
        # resolve to a fresh re-imported duplicate while the already-bound
        # ``run_maintenance_agent`` keeps using the original namespace.
        # Patching ``__globals__`` targets exactly what the function reads.
        glob = run_maintenance_agent.__globals__
        captured: dict[str, Path] = {}
        real_make_clone = glob["make_clone_repo_tool"]

        def cap_make_clone(settings, root):
            captured["tmpdir"] = root
            return real_make_clone(settings, root)

        monkeypatch.setitem(glob, "make_clone_repo_tool", cap_make_clone)
        # No real meta workspace / LLM agent / remote resolution.
        monkeypatch.setitem(
            glob,
            "_build_meta_investigation_workspace",
            lambda ctx, ticket, ws, draft: (None, [], None),
        )
        monkeypatch.setattr(
            base, "build_agent_from_definition", lambda *a, **k: SimpleNamespace()
        )
        monkeypatch.setattr(base, "_safe_close", lambda agent: None)
        monkeypatch.setattr(
            "robotsix_mill.forge.auth._resolve_remote_url",
            lambda settings, rc: "https://github.com/test/test.git",
        )

        seen: dict[str, bool] = {}

        def fake_run_agent(agent, fn, *, what):
            # The clone target must already exist when the agent runs.
            seen["clone_exists"] = (captured["tmpdir"] / "repo").exists()
            return SimpleNamespace(output=MaintenanceResult(success=True, note="ok"))

        monkeypatch.setattr(retry_mod, "run_agent", fake_run_agent)

        result = run_maintenance_agent(ticket, ctx)

        assert seen.get("clone_exists") is True
        assert result.success is True
        db.reset_engine()

    def test_vanished_clone_degrades_gracefully(self, tmp_path, monkeypatch):
        """When a tool touches the clone path in a later turn after the
        ephemeral clone was removed (or a clone_repo failed), the
        FileNotFoundError must be caught and returned as a clean
        MaintenanceResult(success=False) instead of propagating as a Fatal
        block with a raw traceback (live class: cross-repo extraction
        tickets, ``/tmp/maintenance_*/repo``)."""
        from types import SimpleNamespace

        from robotsix_mill.core import db
        from robotsix_mill.agents import base
        from robotsix_mill.agents import retry as retry_mod
        from robotsix_mill.config import RepoConfig
        from robotsix_mill.core.service import TicketService
        from robotsix_mill.stages import StageContext

        s = _settings(tmp_path)
        db.reset_engine()
        db.init_db(s, board_id="test-board")
        service = TicketService(s, board_id="test-board")
        repo_config = RepoConfig(
            repo_id="test-repo",
            langfuse_project_name="test-project",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        )
        ticket = service.create("Extract helper into other-repo", "cross-repo work")
        service.workspace(ticket).repo_dir.mkdir(parents=True, exist_ok=True)
        ctx = StageContext(settings=s, service=service, repo_config=repo_config)

        glob = run_maintenance_agent.__globals__
        monkeypatch.setitem(
            glob,
            "_build_meta_investigation_workspace",
            lambda ctx, ticket, ws, draft: (None, [], None),
        )
        monkeypatch.setattr(
            base, "build_agent_from_definition", lambda *a, **k: SimpleNamespace()
        )
        monkeypatch.setattr(base, "_safe_close", lambda agent: None)
        monkeypatch.setattr(
            "robotsix_mill.forge.auth._resolve_remote_url",
            lambda settings, rc: "https://github.com/test/test.git",
        )

        def fake_run_agent(agent, fn, *, what):
            raise FileNotFoundError(
                2, "No such file or directory", "/tmp/maintenance_x/repo"
            )

        monkeypatch.setattr(retry_mod, "run_agent", fake_run_agent)

        result = run_maintenance_agent(ticket, ctx)

        assert result.success is False
        assert "could not access the repository clone" in result.note
        db.reset_engine()
