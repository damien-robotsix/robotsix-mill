"""Dedicated tests for the implement coordinator — ImplementResult
typo-absorption validator and run_coordinator argument/prompt paths.

Fills coverage gaps identified in
``tests/agents/test_agents_rework.py`` — does NOT duplicate existing
tests for the happy path, tools, or ValidationResult.decide.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai import PromptedOutput
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import UsageLimits

from robotsix_mill.agents.coordinating import (
    ImplementResult,
    run_coordinator,
)
from robotsix_mill.config import Settings


def _settings(tmp_path, **env):
    """Minimal settings helper — mirrors test_agents_rework.py."""
    env.setdefault("MILL_DATA_DIR", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
    return Settings(**env)


# ------------------------------------------------------------------


class TestImplementResult:
    """Pure unit tests for ``ImplementResult._absorb_summary_typos``.

    The validator absorbs known LLM key-typos so pydantic-ai
    strict-validation failures don't trigger $1+ retries.  Every test
    calls the classmethod directly — no mocks needed.
    """

    # -- passthrough & edge cases ----------------------------------------

    def test_canonical_summary_present(self):
        """Canonical ``summary`` present → passes through unchanged."""
        data = {"summary": "done"}
        result = ImplementResult._absorb_summary_typos(data)
        assert result["summary"] == "done"

    def test_non_dict_input_unchanged(self):
        """Non-dict input is returned as-is."""
        data = "not a dict"
        result = ImplementResult._absorb_summary_typos(data)
        assert result == "not a dict"

    def test_empty_dict(self):
        """Empty dict → still empty, no summary key injected."""
        data: dict = {}
        result = ImplementResult._absorb_summary_typos(data)
        assert result == {}
        assert "summary" not in result

    def test_only_updated_memory_present(self):
        """Only ``updated_memory`` → summary stays falsy."""
        data = {"updated_memory": "notes"}
        result = ImplementResult._absorb_summary_typos(data)
        assert not result.get("summary")
        assert result["updated_memory"] == "notes"

    # -- Tier 1 near-miss keys (parametrized) ----------------------------

    @pytest.mark.parametrize("key", [
        "summary_text",
        "summary_str",
        "summaryText",
        "result_summary",
        "text",
        "result",
        "output",
    ])
    def test_tier1_near_miss_absorbed(self, key):
        """Each Tier-1 near-miss key is absorbed into ``summary``."""
        data = {key: "x"}
        result = ImplementResult._absorb_summary_typos(data)
        assert result["summary"] == "x"

    def test_tier1_priority_first_match_wins(self):
        """When multiple Tier-1 keys are present the first in priority
        order wins."""
        data = {"summary_text": "first", "text": "second"}
        result = ImplementResult._absorb_summary_typos(data)
        assert result["summary"] == "first"

    # -- Tier 2 fallback -------------------------------------------------

    def test_tier2_fallback_picks_longest(self):
        """Tier-2 fallback picks the longest non-``updated_memory``
        string value."""
        data = {"foo": "short", "bar": "the longer value"}
        result = ImplementResult._absorb_summary_typos(data)
        assert result["summary"] == "the longer value"

    def test_updated_memory_excluded_from_tier2(self):
        """``updated_memory`` is excluded from Tier-2 candidates."""
        data = {"foo": "summary text", "updated_memory": "notes"}
        result = ImplementResult._absorb_summary_typos(data)
        assert result["summary"] == "summary text"
        assert result["updated_memory"] == "notes"

    def test_tier1_wins_over_tier2(self):
        """Tier-1 match takes priority over any Tier-2 candidate."""
        data = {"result": "a", "foo": "longer value here"}
        result = ImplementResult._absorb_summary_typos(data)
        assert result["summary"] == "a"

    # -- empty / whitespace NOT absorbed ---------------------------------

    def test_empty_string_not_absorbed(self):
        """Empty string near-miss values are NOT absorbed."""
        data = {"text": ""}
        result = ImplementResult._absorb_summary_typos(data)
        assert not result.get("summary")

    def test_whitespace_only_not_absorbed(self):
        """Whitespace-only near-miss values are NOT absorbed."""
        data = {"text": "   "}
        result = ImplementResult._absorb_summary_typos(data)
        assert not result.get("summary")


# ------------------------------------------------------------------


class TestRunCoordinator:
    """Integration tests for ``run_coordinator`` argument forwarding
    and prompt construction.

    All external seams are monkeypatched — no LLM, filesystem, or
    network access.  Each test calls ``run_coordinator`` and asserts
    on the captured arguments or user-prompt text.
    """

    @pytest.fixture(autouse=True)
    def _mock_seams(self, monkeypatch, tmp_path):
        """Replace every seam that ``run_coordinator`` touches so it
        runs entirely in-process.  Captured values are stored on
        ``self.captured`` for per-test assertions."""
        # ── captured state ─────────────────────────────────────────
        self.captured: dict = {}
        self.user_prompt: str | None = None
        self.message_history_passed: list | None = None

        # ── build_agent (robotsix_mill.agents.base) ─────────────────
        from robotsix_mill.agents import base as _base

        def _fake_build_agent(
            settings, *, system_prompt, output_type=None,
            tools=None, web=False, report_issue=True,
            model_name=None, name=None, retries=2,
        ):
            self.captured["system_prompt"] = system_prompt
            self.captured["output_type"] = output_type
            self.captured["tools"] = tools
            self.captured["web"] = web
            self.captured["name"] = name
            self.captured["model_name"] = model_name

            class _FakeAgent:
                @staticmethod
                def run_sync(prompt, *, usage_limits=None,
                             message_history=None):
                    self.captured["usage_limits"] = usage_limits
                    self.captured["message_history"] = message_history
                    self.captured["user_prompt"] = prompt

                    class _R:
                        pass
                    r = _R()
                    r.output = ImplementResult(summary="ok")
                    return r

                @staticmethod
                def close():
                    pass

            return _FakeAgent()

        monkeypatch.setattr(_base, "build_agent", _fake_build_agent)
        # _safe_close just calls agent.close() — our _FakeAgent.close
        # is already a no-op, so no explicit patch is needed.

        # ── build_fs_tools (robotsix_mill.agents.fs_tools) ──────────
        from robotsix_mill.agents import fs_tools as _fs

        def _fake_build_fs_tools(root, settings, *, pre_seeded=None, extra_roots=None):
            self.captured["fs_pre_seeded"] = pre_seeded
            # Return a single read_file tool so the filtering in
            # run_coordinator doesn't blow up.
            def _read_file(path, offset=1, limit=None):
                return "fake content"
            _read_file.__name__ = "read_file"
            return [_read_file]

        monkeypatch.setattr(_fs, "build_fs_tools", _fake_build_fs_tools)

        # ── make_explore_tool (robotsix_mill.agents.explore) ────────
        from robotsix_mill.agents import explore as _expl

        def _fake_make_explore_tool(settings, repo_dir, extra_roots=None):
            def _explore(question):
                return "explored"
            return _explore

        monkeypatch.setattr(
            _expl, "make_explore_tool", _fake_make_explore_tool,
        )

    # -- helper ----------------------------------------------------------

    def _run(self, settings, tmp_path, **kwargs):
        """Call ``run_coordinator`` with (almost) every argument
        defaulted so individual tests only override what they need."""
        defaults: dict = dict(
            settings=settings, repo_dir=tmp_path, spec="do X",
        )
        defaults.update(kwargs)
        return run_coordinator(**defaults)

    # -- feedback paths --------------------------------------------------

    def test_feedback_as_test_failure(self, settings, tmp_path):
        """Non-review ``feedback`` is appended as a ``<test_failure>``
        block."""
        self._run(
            settings, tmp_path, feedback="test_x failed",
        )
        prompt: str = self.captured["user_prompt"]
        assert "<test_failure>" in prompt
        assert "test_x failed" in prompt
        assert "Fix exactly this failure and stop." in prompt

    def test_feedback_as_review_prepended_before_spec(
        self, settings, tmp_path,
    ):
        """``[REVIEW`` feedback is prepended before ``<ticket_spec>``."""
        self._run(
            settings, tmp_path, feedback="[REVIEW] fix docstring",
        )
        prompt: str = self.captured["user_prompt"]
        assert prompt.startswith("<review_feedback>")
        assert "fix docstring" in prompt
        # The review block must appear before ticket_spec.
        review_pos = prompt.index("<review_feedback>")
        spec_pos = prompt.index("<ticket_spec>")
        assert review_pos < spec_pos

    # -- epic_context ----------------------------------------------------

    def test_epic_context_prepended(self, settings, tmp_path):
        """``epic_context`` appears before ``<ticket_spec>``."""
        self._run(
            settings, tmp_path,
            epic_context="## Epic goal",
            spec="do X",
        )
        prompt: str = self.captured["user_prompt"]
        epic_pos = prompt.index("## Epic goal")
        spec_pos = prompt.index("<ticket_spec>")
        assert epic_pos < spec_pos
        assert "do X" in prompt

    # -- memory in prompt ------------------------------------------------

    def test_memory_in_prompt(self, settings, tmp_path):
        """Non-empty memory is wrapped in ``<memory>`` tags."""
        self._run(
            settings, tmp_path,
            memory="gotcha: use Path, not str",
        )
        prompt: str = self.captured["user_prompt"]
        assert "<memory>" in prompt
        assert "gotcha: use Path, not str" in prompt
        assert "</memory>" in prompt

    def test_empty_memory_defaults_to_placeholder(
        self, settings, tmp_path,
    ):
        """Empty ``memory`` (default) yields the start-a-new-ledger
        placeholder."""
        self._run(settings, tmp_path, memory="")
        prompt: str = self.captured["user_prompt"]
        assert "<memory>" in prompt
        assert "(empty — start a new ledger)" in prompt
        assert "</memory>" in prompt

    # -- reference_files / message_history ------------------------------

    def test_reference_files_synthetic_history(
        self, settings, tmp_path,
    ):
        """``reference_files`` with no ``message_history`` and no
        ``feedback`` produces synthetic ``ModelResponse``/``ModelRequest``
        pairs and seeds the fs-tools cache."""
        ref = [{"path": "foo.py", "content": "x=1"}]
        self._run(settings, tmp_path, reference_files=ref)

        # fs_tools cache seeded with resolved path
        pre_seeded = self.captured["fs_pre_seeded"]
        assert pre_seeded is not None
        resolved = (tmp_path / "foo.py").resolve()
        assert resolved in pre_seeded
        assert pre_seeded[resolved] == "x=1"

        # Synthetic message_history passed to run_sync
        mh = self.captured["message_history"]
        assert isinstance(mh, list)
        assert len(mh) == 2  # one response + one request

        # First: ModelResponse with ToolCallPart for read_file
        resp = mh[0]
        assert isinstance(resp, ModelResponse)
        assert len(resp.parts) == 1
        tc = resp.parts[0]
        assert isinstance(tc, ToolCallPart)
        assert tc.tool_name == "read_file"
        assert tc.args == {"path": "foo.py", "offset": 1, "limit": None}
        assert tc.tool_call_id == "preload_foo.py"

        # Second: ModelRequest with ToolReturnPart
        req = mh[1]
        assert isinstance(req, ModelRequest)
        assert len(req.parts) == 1
        tr = req.parts[0]
        assert isinstance(tr, ToolReturnPart)
        assert tr.tool_name == "read_file"
        assert tr.content == "x=1"
        assert tr.tool_call_id == "preload_foo.py"

    def test_reference_files_with_message_history_passthrough(
        self, settings, tmp_path,
    ):
        """When ``message_history`` is provided it is passed directly
        to ``run_sync`` (no synthesis, no fs-cache pre-seeding —
        ``pre_seeded`` is only built when ``message_history is None``)."""
        ref = [{"path": "bar.py", "content": "y=2"}]
        existing_mh = ["existing"]
        self._run(
            settings, tmp_path,
            reference_files=ref,
            message_history=existing_mh,
        )

        # pre_seeded NOT populated when message_history is provided
        assert self.captured["fs_pre_seeded"] is None

        # message_history passed directly, not synthesised
        assert self.captured["message_history"] == existing_mh

    def test_reference_files_with_feedback_no_synthesis(
        self, settings, tmp_path,
    ):
        """``reference_files`` + ``feedback`` → no synthetic history
        (retry path); feedback block present."""
        ref = [{"path": "baz.py", "content": "z=3"}]
        self._run(
            settings, tmp_path,
            reference_files=ref,
            feedback="assertion failed in test_z",
        )

        # No synthetic history on retry
        assert self.captured["message_history"] is None

        # But pre_seeded is still built
        pre_seeded = self.captured["fs_pre_seeded"]
        assert pre_seeded is not None
        resolved = (tmp_path / "baz.py").resolve()
        assert pre_seeded[resolved] == "z=3"

        # feedback block in prompt
        assert "assertion failed in test_z" in self.captured["user_prompt"]

    # -- model_name ------------------------------------------------------

    def test_explicit_model_name_forwarded(self, settings, tmp_path):
        """Explicit ``model_name`` is passed to ``build_agent``."""
        self._run(
            settings, tmp_path,
            model_name="anthropic/sonnet",
        )
        assert self.captured["model_name"] == "anthropic/sonnet"

    def test_model_name_none_uses_settings_model(
        self, settings, tmp_path,
    ):
        """When ``model_name`` is None, ``settings.model`` is used."""
        self._run(settings, tmp_path, model_name=None)
        assert self.captured["model_name"] == settings.model

    # -- fixed build_agent args ------------------------------------------

    def test_build_agent_fixed_args(self, settings, tmp_path):
        """``build_agent`` receives the fixed kwargs that don't vary
        across invocations."""
        self._run(settings, tmp_path)

        assert self.captured["web"] is True
        assert self.captured["name"] == "implement"
        assert isinstance(self.captured["output_type"], PromptedOutput)
        assert self.captured["output_type"].outputs is ImplementResult

        from robotsix_mill.agents.yaml_loader import load_agent_definition

        definition = load_agent_definition(
            Path(__file__).parent.parent.parent / "agent_definitions" / "implement.yaml"
        )
        assert self.captured["system_prompt"] == definition.system_prompt

    # -- usage_limits ----------------------------------------------------

    def test_usage_limits_uses_coordinator_request_limit(
        self, settings, tmp_path,
    ):
        """The ``request_limit`` on the ``UsageLimits`` passed to
        ``run_sync`` comes from ``settings.coordinator_request_limit``."""
        s = _settings(tmp_path, MILL_COORDINATOR_REQUEST_LIMIT="12")
        self._run(s, tmp_path)
        ul = self.captured["usage_limits"]
        assert isinstance(ul, UsageLimits)
        assert ul.request_limit == 12
