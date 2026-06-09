"""Tests for the maintenance agent module."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pydantic_ai
import pydantic_ai.providers.openrouter as orp
import pytest

from robotsix_mill.agents.maintenance import (
    MaintenanceResult,
    make_create_repo_tool,
    make_fork_repo_tool,
    make_investigate_tool,
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

    def test_investigate_stub_returns_error(self, tmp_path):
        """The stub returns a 'not yet implemented' error."""
        s = _settings(tmp_path)
        fn = make_investigate_tool(s)
        result = fn("what is X?", "https://example.com/repo")
        assert "not yet implemented" in result
        assert "investigate" in result

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

        # investigate
        r3 = make_investigate_tool(s)(question="q", repo_url="https://example.com/r")
        assert "not yet implemented" in r3

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
        make_investigate_tool(s)

        # Also register post_findings by calling its factory
        from robotsix_mill.agents.maintenance import make_post_findings_tool

        make_post_findings_tool(s, agent_name="test")

        names = {t.name for t in ToolRegistry.list_tools()}
        assert "create_repo" in names
        assert "fork_repo" in names
        assert "investigate" in names
        assert "post_findings" in names


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
        assert definition.ask_user is False
        assert definition.report_issue is True
        assert definition.retries == 2
        assert definition.output_type == "MaintenanceResult"
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
            "robotsix_mill.agents.openrouter_cost.CostInstrumentedOpenRouterModel",
            FakeModel,
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
        assert "investigate" in tool_names
        assert "post_findings" in tool_names

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
            "robotsix_mill.agents.openrouter_cost.CostInstrumentedOpenRouterModel",
            FakeModel,
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
        """Sanity check: no leftover mock module from the stage test
        (it uses a try/finally to remove its injection, but this
        confirms no leak)."""
        assert "robotsix_mill.agents.maintenance" in sys.modules
        mod = sys.modules["robotsix_mill.agents.maintenance"]
        # Should be the real module, not a mock
        assert not isinstance(mod, ModuleType) or hasattr(mod, "MaintenanceResult")
