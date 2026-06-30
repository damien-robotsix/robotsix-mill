"""Tests for the ``spawn_subtask`` tool factory + runner.

The sub-agent runs to completion (or hits its own budget) inside one
``agent.run_sync`` call. The contract this module guards:

- The sub-agent's per-call request budget is set from
  ``settings.subtask_request_limit`` — independent of the parent
  coordinator's budget. A parent's 200-cap can't be drained by a
  single misbehaving sub-agent.
- The sub-agent's tool palette is read/write/edit/list/explore/
  run_command — same as the parent — but explicitly excludes
  ``web_research`` (no need; would inflate context), ``consult_expert``
  and ``spawn_subtask`` itself (no recursion / fan-out).
- Budget exceptions translate to a structured "subtask incomplete:
  budget cap reached" string the parent can act on, never bubble.
- Other exceptions also translate to a "subtask failed: …" string
  rather than propagating into the parent's loop.
"""

from __future__ import annotations

import asyncio

import pytest

from robotsix_mill.agents import spawn_subtask as _ss
from robotsix_mill.config import Settings, _reset_secrets


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    _reset_secrets()
    return Settings(data_dir=str(tmp_path), subtask_request_limit=7)


@pytest.fixture
def repo_dir(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "hello.py").write_text("print('hi')\n")
    return tmp_path


class TestRunSpawnSubtask:
    def test_returns_agent_output_on_happy_path(
        self,
        settings,
        repo_dir,
        monkeypatch,
    ):
        """The sub-agent's final string output is returned to the
        parent verbatim. The summary IS the contract — it's how the
        parent decides what to do next."""
        captured: dict = {}

        class _FakeResult:
            output = "moved 3 files, updated 5 imports"

        class _FakeAgent:
            async def run(self, user_prompt, *, usage_limits=None):
                captured["user_prompt"] = user_prompt
                captured["usage_limits"] = usage_limits
                return _FakeResult()

            def close(self):
                pass

        from robotsix_mill.agents import base as _base

        monkeypatch.setattr(_base, "build_agent", lambda *a, **kw: _FakeAgent())

        out = asyncio.run(
            _ss.run_spawn_subtask(
                settings=settings,
                repo_dir=repo_dir,
                name="move-runners",
                prompt="Move every *_runner.py into runners/.",
                files_in_scope=["src/robotsix_mill/runners/periodic_runner.py"],
            )
        )

        assert out == "moved 3 files, updated 5 imports"
        # The sub-agent's request budget came from settings, not the
        # parent's coordinator_request_limit.
        assert captured["usage_limits"].request_limit == 7
        # files_in_scope landed in the user prompt as a hint block.
        assert "files-in-scope" in captured["user_prompt"]
        assert "periodic_runner.py" in captured["user_prompt"]

    def test_budget_cap_returns_structured_string(
        self,
        settings,
        repo_dir,
        monkeypatch,
    ):
        """A sub-agent that exhausts its per-subtask budget MUST NOT
        bubble UsageLimitExceeded into the parent's loop — that would
        re-trigger the parent's own retry / failure handling and
        defeat the purpose of bounded delegation. Translate to a
        'subtask incomplete:' string the parent can decide on."""
        from pydantic_ai.exceptions import UsageLimitExceeded

        class _FakeAgent:
            async def run(self, user_prompt, *, usage_limits=None):
                raise UsageLimitExceeded("budget cap")

            def close(self):
                pass

        from robotsix_mill.agents import base as _base

        monkeypatch.setattr(_base, "build_agent", lambda *a, **kw: _FakeAgent())

        out = asyncio.run(
            _ss.run_spawn_subtask(
                settings=settings,
                repo_dir=repo_dir,
                name="too-big-subtask",
                prompt="...",
            )
        )

        assert out.startswith("subtask incomplete: budget cap reached")
        # The number in the message is the per-subtask cap, NOT the
        # parent's coordinator cap — operators reading logs need to
        # know which budget tripped.
        assert "7 requests" in out

    def test_other_exception_returns_structured_string(
        self,
        settings,
        repo_dir,
        monkeypatch,
    ):
        """Same contract for non-budget errors. A networked LLM call
        that 503s mid-subtask must come back as a 'subtask failed'
        string, not a runtime crash the parent's coordinator has to
        catch ad-hoc."""

        class _FakeAgent:
            async def run(self, user_prompt, *, usage_limits=None):
                raise RuntimeError("LLM upstream 503")

            def close(self):
                pass

        from robotsix_mill.agents import base as _base

        monkeypatch.setattr(_base, "build_agent", lambda *a, **kw: _FakeAgent())

        out = asyncio.run(
            _ss.run_spawn_subtask(
                settings=settings,
                repo_dir=repo_dir,
                name="x",
                prompt="...",
            )
        )

        assert out.startswith("subtask failed: ")
        assert "RuntimeError" in out
        assert "LLM upstream 503" in out

    def test_empty_output_replaced_with_placeholder(
        self,
        settings,
        repo_dir,
        monkeypatch,
    ):
        """A sub-agent that returns the empty string would look like
        'success with no work done' to the parent. Replace with an
        explicit marker so the parent can distinguish that case from
        a real summary."""

        class _FakeResult:
            output = ""

        class _FakeAgent:
            async def run(self, user_prompt, *, usage_limits=None):
                return _FakeResult()

            def close(self):
                pass

        from robotsix_mill.agents import base as _base

        monkeypatch.setattr(_base, "build_agent", lambda *a, **kw: _FakeAgent())

        out = asyncio.run(
            _ss.run_spawn_subtask(
                settings=settings,
                repo_dir=repo_dir,
                name="x",
                prompt="...",
            )
        )

        assert "empty summary" in out


class TestMakeSpawnSubtaskTool:
    def test_tool_factory_returns_callable(self, settings, repo_dir):
        """The factory binds settings + repo_dir and returns a
        callable shaped for pydantic-ai's tool surface — name,
        positional args, type annotations."""
        tool = _ss.make_spawn_subtask_tool(settings, repo_dir)
        assert callable(tool)
        assert tool.__name__ == "spawn_subtask"

    def test_tool_registers_in_tool_registry(self, settings, repo_dir):
        """The factory side-effects a ToolInfo into the global
        registry so the operator-facing /tools page lists it
        alongside consult_expert. Guard against a future refactor
        that quietly drops the registration."""
        from robotsix_mill.agents.tool_registry import ToolRegistry

        # Build once to trigger registration.
        _ss.make_spawn_subtask_tool(settings, repo_dir)

        names = [t.name for t in ToolRegistry.list_tools()]
        assert "spawn_subtask" in names
