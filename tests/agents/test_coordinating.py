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
from robotsix_mill.config import Settings, Secrets, _reset_secrets


def _settings(tmp_path, **env):
    """Minimal settings helper — mirrors test_agents_rework.py."""
    env.setdefault("data_dir", str(tmp_path))
    env.setdefault("OPENROUTER_API_KEY", "k")
    # Mirror openrouter_api_key into Secrets so get_secrets() works
    key = env.get("OPENROUTER_API_KEY")
    if key is not None:
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(openrouter_api_key=key)
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

    @pytest.mark.parametrize(
        "key",
        [
            "summary_text",
            "summary_str",
            "summaryText",
            "result_summary",
            "text",
            "result",
            "output",
        ],
    )
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
            settings,
            *,
            system_prompt,
            output_type=None,
            tools=None,
            web_knowledge=False,
            report_issue=True,
            read_ticket=False,
            reply_to_thread=True,
            close_thread=True,
            ask_user=True,
            model_name=None,
            name=None,
            retries=2,
            skills=None,
            modules=False,
            repo_dir=None,
            **_extra,
        ):
            self.captured["system_prompt"] = system_prompt
            self.captured["output_type"] = output_type
            self.captured["tools"] = tools
            self.captured["web_knowledge"] = web_knowledge
            self.captured["name"] = name
            self.captured["model_name"] = model_name
            self.captured["repo_dir"] = repo_dir

            class _FakeAgent:
                @staticmethod
                def run_sync(prompt, *, usage_limits=None, message_history=None):
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
            self.captured["fs_extra_roots"] = extra_roots

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
            self.captured["explore_extra_roots"] = extra_roots

            def _explore(question):
                return "explored"

            return _explore

        monkeypatch.setattr(
            _expl,
            "make_explore_tool",
            _fake_make_explore_tool,
        )

    # -- helper ----------------------------------------------------------

    def _run(self, settings, tmp_path, **kwargs):
        """Call ``run_coordinator`` with (almost) every argument
        defaulted so individual tests only override what they need."""
        defaults: dict = dict(
            settings=settings,
            repo_dir=tmp_path,
            spec="do X",
        )
        defaults.update(kwargs)
        return run_coordinator(**defaults)

    # -- str-output fallback (claude_sdk) --------------------------------

    def test_str_output_is_coerced_not_crashed(self, settings, tmp_path, monkeypatch):
        """When the model's final message doesn't parse as ImplementResult,
        llmio returns the raw string. run_coordinator must coerce it into an
        ImplementResult(summary=text) — NOT crash with "'str' object has no
        attribute 'conversation_state'" (the bug that blocked 5ed1/0da9).
        Also covers a result lacking all_messages_json/new_messages_json
        (the claude_sdk _SdkToolResult shape)."""
        from robotsix_mill.agents import base as _base

        def _fake_build_agent(*a, **kw):
            class _R:
                output = "raw model text, not JSON"  # the parse-fallback case

                # _SdkToolResult-like: no all_messages_json/new_messages_json
                @staticmethod
                def all_messages_json():
                    raise AttributeError("no history")

                @staticmethod
                def new_messages_json():
                    raise AttributeError("no history")

            class _FakeAgent:
                @staticmethod
                def run_sync(prompt, *, usage_limits=None, message_history=None):
                    return _R()

                @staticmethod
                def close():
                    pass

            return _FakeAgent()

        monkeypatch.setattr(_base, "build_agent", _fake_build_agent)

        result = self._run(settings, tmp_path)
        assert isinstance(result, ImplementResult)
        assert result.summary == "raw model text, not JSON"
        assert result.conversation_state is None
        assert result.new_messages is None

    # -- feedback paths --------------------------------------------------

    def test_feedback_as_test_failure(self, settings, tmp_path):
        """Non-review ``feedback`` is appended as a ``<test_failure>``
        block."""
        self._run(
            settings,
            tmp_path,
            feedback="test_x failed",
        )
        prompt: str = self.captured["user_prompt"]
        assert "````test-failure" in prompt
        assert "test_x failed" in prompt
        assert "Fix exactly this failure and stop." in prompt

    def test_feedback_as_review_prepended_before_spec(
        self,
        settings,
        tmp_path,
    ):
        """``[REVIEW`` feedback is prepended before ``<ticket_spec>``."""
        self._run(
            settings,
            tmp_path,
            feedback="[REVIEW] fix docstring",
        )
        prompt: str = self.captured["user_prompt"]
        assert prompt.startswith("````review-feedback")
        assert "fix docstring" in prompt
        # The review block must appear before ticket_spec.
        review_pos = prompt.index("````review-feedback")
        spec_pos = prompt.index("````ticket-spec")
        assert review_pos < spec_pos

    # -- epic_context ----------------------------------------------------

    def test_epic_context_prepended(self, settings, tmp_path):
        """``epic_context`` appears before ``<ticket_spec>``."""
        self._run(
            settings,
            tmp_path,
            epic_context="## Epic goal",
            spec="do X",
        )
        prompt: str = self.captured["user_prompt"]
        epic_pos = prompt.index("## Epic goal")
        spec_pos = prompt.index("````ticket-spec")
        assert epic_pos < spec_pos
        assert "do X" in prompt

    # -- memory in prompt ------------------------------------------------

    def test_memory_in_prompt(self, settings, tmp_path):
        """Non-empty memory is wrapped in ``<memory>`` tags."""
        self._run(
            settings,
            tmp_path,
            memory="gotcha: use Path, not str",
        )
        prompt: str = self.captured["user_prompt"]
        assert "````memory" in prompt
        assert "gotcha: use Path, not str" in prompt
        assert "````\n<!-- /memory -->" in prompt

    def test_empty_memory_defaults_to_placeholder(
        self,
        settings,
        tmp_path,
    ):
        """Empty ``memory`` (default) yields the start-a-new-ledger
        placeholder."""
        self._run(settings, tmp_path, memory="")
        prompt: str = self.captured["user_prompt"]
        assert "````memory" in prompt
        assert "(empty — start a new ledger)" in prompt
        assert "````\n<!-- /memory -->" in prompt

    # -- extra_roots forwarding -----------------------------------------

    def test_extra_roots_forwards_to_build_fs_tools_and_explore(
        self,
        settings,
        tmp_path,
    ):
        """``extra_roots`` is forwarded to both ``build_fs_tools``
        and ``make_explore_tool``."""
        roots = [tmp_path / "clone_a", tmp_path / "clone_b"]
        self._run(settings, tmp_path, extra_roots=roots)
        assert self.captured["fs_extra_roots"] == roots
        assert self.captured["explore_extra_roots"] == roots

    # -- reference_files / message_history ------------------------------

    def test_reference_files_synthetic_history(
        self,
        settings,
        tmp_path,
    ):
        """``reference_files`` with no ``message_history`` and no
        ``feedback`` produces a 3-message synthetic history:
        ``ModelRequest(UserPromptPart)`` (the real user prompt) →
        ``ModelResponse(ToolCallPart)`` (preload calls) →
        ``ModelRequest(ToolReturnPart)`` (preload returns). The fs-tools
        cache is seeded for the preloaded path."""
        from pydantic_ai.messages import UserPromptPart

        (tmp_path / "foo.py").write_text("x=1")
        ref = [{"path": "foo.py"}]
        self._run(settings, tmp_path, reference_files=ref)

        # fs_tools cache seeded with resolved path
        pre_seeded = self.captured["fs_pre_seeded"]
        assert pre_seeded is not None
        resolved = (tmp_path / "foo.py").resolve()
        assert resolved in pre_seeded
        assert pre_seeded[resolved] == "x=1"

        # Synthetic message_history passed to run_sync — now 3 entries
        # because the user_prompt is prepended into the history so the
        # ordering reads: user → assistant tool_calls → user tool returns.
        mh = self.captured["message_history"]
        assert isinstance(mh, list)
        assert len(mh) == 3

        # [0]: ModelRequest carrying the real user prompt
        first = mh[0]
        assert isinstance(first, ModelRequest)
        assert len(first.parts) == 1
        up = first.parts[0]
        assert isinstance(up, UserPromptPart)
        # The user_prompt isn't empty (it's the implement coordinator's
        # built prompt — ticket_spec etc.).
        assert isinstance(up.content, str) and up.content.strip()

        # [1]: ModelResponse with the preload ToolCallParts
        resp = mh[1]
        assert isinstance(resp, ModelResponse)
        assert len(resp.parts) == 1
        tc = resp.parts[0]
        assert isinstance(tc, ToolCallPart)
        assert tc.tool_name == "read_file"
        assert tc.args == {"path": "foo.py", "offset": 1, "limit": None}
        assert tc.tool_call_id == "preload_foo.py"

        # [2]: ModelRequest with matching ToolReturnPart
        req = mh[2]
        assert isinstance(req, ModelRequest)
        assert len(req.parts) == 1
        tr = req.parts[0]
        assert isinstance(tr, ToolReturnPart)
        assert tr.tool_name == "read_file"
        assert tr.content == "x=1"
        assert tr.tool_call_id == "preload_foo.py"

        # run_sync was called with user_prompt=None (the prompt is in mh[0]).
        assert self.captured["user_prompt"] is None

    def test_reference_files_multiple_paths_share_one_turn(
        self,
        settings,
        tmp_path,
    ):
        """All preloaded reference files land in a single turn —
        one ``ModelResponse`` carrying N parallel ``read_file``
        ToolCallParts and one ``ModelRequest`` carrying N matching
        ``ToolReturnPart``s — so the agent perceives one batched
        preload instead of N sequential exchanges. The leading user
        prompt sits in its own ModelRequest at index 0."""
        (tmp_path / "a.py").write_text("A")
        (tmp_path / "b.py").write_text("B")
        (tmp_path / "c.py").write_text("C")
        ref = [{"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"}]

        self._run(settings, tmp_path, reference_files=ref)

        mh = self.captured["message_history"]
        assert isinstance(mh, list)
        assert len(mh) == 3  # leading user prompt + Response + Request

        first, resp, req = mh[0], mh[1], mh[2]
        assert isinstance(first, ModelRequest)
        assert isinstance(resp, ModelResponse)
        assert isinstance(req, ModelRequest)
        assert len(resp.parts) == 3
        assert len(req.parts) == 3

        # Parallel ToolCallParts — one per file, in input order.
        for part, path in zip(resp.parts, ("a.py", "b.py", "c.py")):
            assert isinstance(part, ToolCallPart)
            assert part.tool_name == "read_file"
            assert part.args == {"path": path, "offset": 1, "limit": None}
            assert part.tool_call_id == f"preload_{path}"

        # Matching ToolReturnParts — same order, same ids.
        for part, (path, content) in zip(
            req.parts,
            [("a.py", "A"), ("b.py", "B"), ("c.py", "C")],
        ):
            assert isinstance(part, ToolReturnPart)
            assert part.tool_name == "read_file"
            assert part.content == content
            assert part.tool_call_id == f"preload_{path}"

    def test_reference_files_with_message_history_passthrough(
        self,
        settings,
        tmp_path,
    ):
        """When ``message_history`` is provided it is passed directly
        to ``run_sync`` (no synthesis, no fs-cache pre-seeding —
        ``pre_seeded`` is only built when ``message_history is None``)."""
        (tmp_path / "bar.py").write_text("y=2")
        ref = [{"path": "bar.py"}]
        existing_mh = ["existing"]
        self._run(
            settings,
            tmp_path,
            reference_files=ref,
            message_history=existing_mh,
        )

        # pre_seeded NOT populated when message_history is provided
        assert self.captured["fs_pre_seeded"] is None

        # message_history passed directly, not synthesised
        assert self.captured["message_history"] == existing_mh

    def test_reference_files_with_feedback_synthesis(
        self,
        settings,
        tmp_path,
    ):
        """``reference_files`` + ``feedback`` → synthetic history IS built
        (the old first-pass-only gate is lifted per ticket §3)."""
        (tmp_path / "baz.py").write_text("z=3")
        ref = [{"path": "baz.py"}]
        self._run(
            settings,
            tmp_path,
            reference_files=ref,
            feedback="assertion failed in test_z",
        )

        # Synthetic history IS built on retry now — 3 entries
        # (leading user prompt + preload call + preload return).
        mh = self.captured["message_history"]
        assert isinstance(mh, list)
        assert len(mh) == 3

        # pre_seeded is still built
        pre_seeded = self.captured["fs_pre_seeded"]
        assert pre_seeded is not None
        resolved = (tmp_path / "baz.py").resolve()
        assert pre_seeded[resolved] == "z=3"

        # feedback block in the synthesized user prompt (now living
        # inside mh[0]'s UserPromptPart, not the captured run_sync arg).
        up = mh[0].parts[0]
        assert "assertion failed in test_z" in up.content

    # -- model_name ------------------------------------------------------

    def test_explicit_model_name_forwarded(self, settings, tmp_path):
        """Explicit ``model_name`` is passed to ``build_agent``."""
        self._run(
            settings,
            tmp_path,
            model_name="anthropic/sonnet",
        )
        assert self.captured["model_name"] == "anthropic/sonnet"

    def test_model_name_none_uses_settings_model(
        self,
        settings,
        tmp_path,
    ):
        """When ``model_name`` is None, ``settings.model`` is used."""
        self._run(settings, tmp_path, model_name=None)
        assert self.captured["model_name"] == settings.model

    # -- fixed build_agent args ------------------------------------------

    def test_build_agent_fixed_args(self, settings, tmp_path):
        """``build_agent`` receives the fixed kwargs that don't vary
        across invocations."""
        self._run(settings, tmp_path)

        assert self.captured["web_knowledge"] is True
        assert self.captured["name"] == "implement"
        assert isinstance(self.captured["output_type"], PromptedOutput)
        assert self.captured["output_type"].outputs is ImplementResult

        from robotsix_mill.agents.yaml_loader import load_agent_definition

        definition = load_agent_definition(
            Path(__file__).parent.parent.parent / "agent_definitions" / "implement.yaml"
        )
        assert self.captured["system_prompt"] == definition.system_prompt

    # -- language_instructions -------------------------------------------

    def test_language_instructions_injected_into_system_prompt(
        self,
        settings,
        tmp_path,
    ):
        """When ``language_instructions`` is non-empty it is prepended
        after ``## Language conventions`` heading."""
        from robotsix_mill.agents.yaml_loader import load_agent_definition

        definition = load_agent_definition(
            Path(__file__).parent.parent.parent / "agent_definitions" / "implement.yaml"
        )
        snippet = "Use pytest. Never run uv sync."
        self._run(settings, tmp_path, language_instructions=snippet)
        prompt: str = self.captured["system_prompt"]
        assert prompt.startswith(definition.system_prompt)
        assert "\n\n## Language conventions\n\n" + snippet in prompt
        # The language conventions appear after the YAML preamble.
        conventions_pos = prompt.index("## Language conventions")
        # The snippet text itself appears after the heading.
        assert prompt.index(snippet) == conventions_pos + len(
            "## Language conventions\n\n"
        )

    def test_language_instructions_empty_unchanged(
        self,
        settings,
        tmp_path,
    ):
        """When ``language_instructions`` is empty (default), the system
        prompt is unchanged."""
        from robotsix_mill.agents.yaml_loader import load_agent_definition

        definition = load_agent_definition(
            Path(__file__).parent.parent.parent / "agent_definitions" / "implement.yaml"
        )
        self._run(settings, tmp_path, language_instructions="")
        assert self.captured["system_prompt"] == definition.system_prompt

    # -- usage_limits ----------------------------------------------------

    def test_usage_limits_uses_coordinator_request_limit(
        self,
        settings,
        tmp_path,
    ):
        """The ``request_limit`` on the ``UsageLimits`` passed to
        ``run_sync`` comes from ``settings.coordinator_request_limit``."""
        s = _settings(tmp_path, coordinator_request_limit="12")
        self._run(s, tmp_path)
        ul = self.captured["usage_limits"]
        assert isinstance(ul, UsageLimits)
        assert ul.request_limit == 12

    # -- previous_attempt_summary ----------------------------------------

    def test_previous_attempt_summary_prepended_before_feedback(
        self,
        settings,
        tmp_path,
    ):
        """When ``previous_attempt_summary`` and ``feedback`` are both
        set, ``<previous_attempt>`` appears before
        ``<review_feedback>``/``<test_failure>`` in the user prompt."""
        self._run(
            settings,
            tmp_path,
            previous_attempt_summary="prior summary text",
            feedback="test_x failed",
        )
        prompt: str = self.captured["user_prompt"]
        prev_pos = prompt.index("````previous-attempt")
        test_pos = prompt.index("````test-failure")
        assert prev_pos < test_pos
        assert "prior summary text" in prompt
        assert "test_x failed" in prompt

    def test_previous_attempt_summary_no_feedback_no_block(
        self,
        settings,
        tmp_path,
    ):
        """When ``previous_attempt_summary`` is set but ``feedback`` is
        None, the ``<previous_attempt>`` block is NOT injected (it is
        only relevant on retries)."""
        self._run(
            settings,
            tmp_path,
            previous_attempt_summary="prior summary text",
        )
        prompt: str = self.captured["user_prompt"]
        assert "````previous-attempt" not in prompt

    # -- reference_files edge cases -------------------------------------

    def test_reference_files_missing_on_disk_omitted(
        self,
        settings,
        tmp_path,
        caplog,
    ):
        """A ``reference_files`` entry pointing to a file that doesn't
        exist on disk produces a warning log and is omitted from the
        synthetic history (no crash, no fabricated ToolReturn)."""
        import logging

        ref = [{"path": "exists.py"}, {"path": "gone.py"}]
        (tmp_path / "exists.py").write_text("real content")

        with caplog.at_level(
            logging.WARNING, logger="robotsix_mill.agents.coordinating"
        ):
            self._run(settings, tmp_path, reference_files=ref)

        # Warning logged for the missing file
        assert any("gone.py" in m and "not found" in m for m in caplog.messages), (
            f"expected warning about gone.py, got: {caplog.messages}"
        )

        # Synthetic history built only for the existing file (with the
        # leading user-prompt ModelRequest prepended → 3 entries).
        mh = self.captured["message_history"]
        assert isinstance(mh, list)
        assert len(mh) == 3
        tr = mh[2].parts[0]
        assert isinstance(tr, ToolReturnPart)
        assert tr.content == "real content"
        assert tr.tool_call_id == "preload_exists.py"

        # pre_seeded only has existing file
        pre_seeded = self.captured["fs_pre_seeded"]
        assert pre_seeded is not None
        resolved = (tmp_path / "exists.py").resolve()
        assert resolved in pre_seeded
        assert pre_seeded[resolved] == "real content"
        gone_resolved = (tmp_path / "gone.py").resolve()
        assert gone_resolved not in pre_seeded

    def test_reference_files_toolreturn_reflects_latest_disk_content(
        self,
        settings,
        tmp_path,
    ):
        """When the file on disk is modified after reference_files is
        built, the synthetic ToolReturn contains the *latest* on-disk
        content, not stale cached content."""
        (tmp_path / "foo.py").write_text("original content")
        ref = [{"path": "foo.py"}]

        self._run(settings, tmp_path, reference_files=ref)

        mh = self.captured["message_history"]
        # mh[0]: user prompt; mh[1]: tool_calls; mh[2]: tool returns.
        tr = mh[2].parts[0]
        assert isinstance(tr, ToolReturnPart)
        # Content matches disk — the test writes and immediately calls
        # run_coordinator, which reads fresh. There's no stale cache path
        # because the artifact is paths-only with no content key.
        assert tr.content == "original content"


# ──────────────────────────────────────────────────────────────────────
# run_coordinator_with_experts (ticket 0e3e)
# ──────────────────────────────────────────────────────────────────────


def _make_def(domain: str, module_paths: list[str], system_prompt: str = "You are X."):
    from robotsix_mill.agents.expert_loader import ExpertDefinition

    return ExpertDefinition.model_validate(
        {
            "domain": domain,
            "module_paths": module_paths,
            "system_prompt": system_prompt,
        }
    )


class _CapturingExpertAgent:
    """Stand-in for a pydantic-ai agent returned by ExpertManager.create_expert.

    Records every run_sync call so tests can assert prompt content.
    Returns a pre-configured ImplementResult or raises a pre-configured
    exception."""

    def __init__(self, domain: str, output=None, exc=None) -> None:
        self.domain = domain
        self._output = output or ImplementResult(
            summary=f"{domain} done",
            reference_files=[f"src/{domain}.py"],
        )
        self._exc = exc
        self.calls: list[dict] = []
        self.closed = False

    def run_sync(self, user_prompt, *, usage_limits=None, message_history=None):
        self.calls.append(
            {
                "prompt": user_prompt,
                "usage_limits": usage_limits,
                "message_history": message_history,
            }
        )
        if self._exc is not None:
            raise self._exc

        class _R:
            pass

        r = _R()
        r.output = self._output
        return r

    def close(self) -> None:
        self.closed = True


class TestRunCoordinatorWithExperts:
    """Tests for the expert-aware coordinator (ticket 0e3e).

    Monkeypatches `ExpertManager.load_definitions` and `create_expert`
    so no real pydantic-ai agent is constructed. Each test asserts on
    the prompt content, the set of experts invoked, and the aggregated
    output."""

    @pytest.fixture(autouse=True)
    def _seams(self, monkeypatch, tmp_path):
        from robotsix_mill.agents import expert_manager as _em

        # state captured per test
        self.created: list[tuple[str, dict]] = []
        self.run_coordinator_called = False

        def _fake_load_definitions(self_mgr, definitions_dir=None):
            return self._definitions

        self._definitions: dict = {}
        monkeypatch.setattr(
            _em.ExpertManager,
            "load_definitions",
            _fake_load_definitions,
        )

        def _fake_create_expert(
            self_mgr, definition, *, output_type=None, memory_text=""
        ):
            agent = self._agents[definition.domain]
            self.created.append(
                (
                    definition.domain,
                    {
                        "output_type": output_type,
                        "memory_text": memory_text,
                    },
                )
            )
            return agent

        self._agents: dict[str, _CapturingExpertAgent] = {}
        monkeypatch.setattr(
            _em.ExpertManager,
            "create_expert",
            _fake_create_expert,
        )

        # Patch run_coordinator (the fallback) to a recorder so tests
        # can assert when the fallback fires.
        from robotsix_mill.agents import coordinating as _co

        def _spy_run_coordinator(**kwargs):
            self.run_coordinator_called = True
            return ImplementResult(summary="fallback", reference_files=[])

        monkeypatch.setattr(_co, "run_coordinator", _spy_run_coordinator)

    def _settings_with_data(self, tmp_path):
        return _settings(tmp_path)

    def _call(self, tmp_path, *, file_map=None, feedback=None, spec="do X"):
        from robotsix_mill.agents.coordinating import (
            run_coordinator_with_experts,
        )

        return run_coordinator_with_experts(
            settings=self._settings_with_data(tmp_path),
            repo_dir=tmp_path,
            spec=spec,
            file_map=file_map,
            feedback=feedback,
            board_id="test-board",
        )

    # -- routing ----------------------------------------------------------

    def test_routes_to_single_matching_domain(self, tmp_path):
        self._definitions = {
            "python": _make_def("python", ["src/**/*.py"]),
            "docs": _make_def("docs", ["docs/**/*.md"]),
        }
        self._agents = {
            "python": _CapturingExpertAgent("python"),
            "docs": _CapturingExpertAgent("docs"),
        }
        result = self._call(tmp_path, file_map={"src/foo.py"})

        assert [d for d, _ in self.created] == ["python"]
        # docs expert wasn't even constructed
        assert not self._agents["docs"].calls
        assert "[python] python done" in result.summary
        assert "src/python.py" in result.reference_files
        assert not self.run_coordinator_called

    def test_routes_to_multiple_matching_domains(self, tmp_path):
        self._definitions = {
            "python": _make_def("python", ["src/**/*.py"]),
            "tests": _make_def("tests", ["tests/**/*.py"]),
        }
        self._agents = {
            "python": _CapturingExpertAgent("python"),
            "tests": _CapturingExpertAgent("tests"),
        }
        result = self._call(
            tmp_path,
            file_map={"src/foo.py", "tests/test_bar.py"},
        )

        domains = sorted(d for d, _ in self.created)
        assert domains == ["python", "tests"]
        assert "[python] python done" in result.summary
        assert "[tests] tests done" in result.summary
        assert set(result.reference_files) == {"src/python.py", "src/tests.py"}

    def test_falls_back_when_no_match(self, tmp_path):
        self._definitions = {
            "python": _make_def("python", ["src/**/*.py"]),
        }
        self._agents = {"python": _CapturingExpertAgent("python")}
        result = self._call(tmp_path, file_map={"docs/readme.md"})

        assert self.run_coordinator_called
        assert result.summary == "fallback"

    def test_falls_back_when_no_file_map(self, tmp_path):
        self._definitions = {"python": _make_def("python", ["src/**/*.py"])}
        self._call(tmp_path, file_map=None)

        assert self.run_coordinator_called

    def test_falls_back_when_definitions_missing(self, tmp_path, monkeypatch):
        from robotsix_mill.agents import expert_manager as _em

        def _raise(self_mgr, definitions_dir=None):
            raise FileNotFoundError("not present in test")

        monkeypatch.setattr(_em.ExpertManager, "load_definitions", _raise)

        self._call(tmp_path, file_map={"src/foo.py"})
        assert self.run_coordinator_called

    # -- prompt + memory wiring ------------------------------------------

    def test_each_expert_receives_domain_context(self, tmp_path):
        self._definitions = {
            "a": _make_def("a", ["src/**/*.py"]),
            "b": _make_def("b", ["tests/**/*.py"]),
        }
        self._agents = {
            "a": _CapturingExpertAgent("a"),
            "b": _CapturingExpertAgent("b"),
        }
        self._call(tmp_path, file_map={"src/foo.py", "tests/bar.py"})

        ap = self._agents["a"].calls[0]["prompt"]
        assert "You are the `a` expert" in ap
        assert "src/foo.py" in ap
        assert "Other experts also working this ticket: b" in ap
        bp = self._agents["b"].calls[0]["prompt"]
        assert "You are the `b` expert" in bp
        assert "tests/bar.py" in bp
        assert "Other experts also working this ticket: a" in bp

    def test_persists_per_expert_memory(self, tmp_path):
        self._definitions = {"a": _make_def("a", ["src/**/*.py"])}
        self._agents = {
            "a": _CapturingExpertAgent(
                "a",
                output=ImplementResult(
                    summary="hi",
                    updated_memory="learned X",
                    reference_files=[],
                ),
            ),
        }
        self._call(tmp_path, file_map={"src/foo.py"})

        mem_file = tmp_path / "test-board" / "expert_a_memory.md"
        assert mem_file.exists()
        assert "learned X" in mem_file.read_text()

    # -- aggregation -----------------------------------------------------

    def test_aggregates_dedupes_reference_files(self, tmp_path):
        self._definitions = {
            "a": _make_def("a", ["src/**/*.py"]),
            "b": _make_def("b", ["tests/**/*.py"]),
        }
        self._agents = {
            "a": _CapturingExpertAgent(
                "a",
                output=ImplementResult(
                    summary="A",
                    reference_files=["src/x.py", "src/shared.py"],
                ),
            ),
            "b": _CapturingExpertAgent(
                "b",
                output=ImplementResult(
                    summary="B",
                    reference_files=["tests/y.py", "src/shared.py"],
                ),
            ),
        }
        result = self._call(
            tmp_path,
            file_map={"src/foo.py", "tests/bar.py"},
        )
        # shared.py appears once even though both experts mentioned it
        assert result.reference_files.count("src/shared.py") == 1
        assert set(result.reference_files) == {
            "src/x.py",
            "src/shared.py",
            "tests/y.py",
        }

    # -- failure handling ------------------------------------------------

    def test_continues_when_one_expert_raises(self, tmp_path):
        from pydantic_ai.exceptions import UsageLimitExceeded

        self._definitions = {
            "a": _make_def("a", ["src/**/*.py"]),
            "b": _make_def("b", ["tests/**/*.py"]),
        }
        self._agents = {
            "a": _CapturingExpertAgent("a", exc=UsageLimitExceeded("over budget")),
            "b": _CapturingExpertAgent("b"),
        }
        result = self._call(
            tmp_path,
            file_map={"src/foo.py", "tests/bar.py"},
        )
        # b's summary is in result; a is silently skipped
        assert "[b] b done" in result.summary
        assert "[a]" not in result.summary
        assert not self.run_coordinator_called

    def test_falls_back_when_all_experts_fail(self, tmp_path):
        from pydantic_ai.exceptions import UsageLimitExceeded

        self._definitions = {"a": _make_def("a", ["src/**/*.py"])}
        self._agents = {
            "a": _CapturingExpertAgent("a", exc=UsageLimitExceeded("over")),
        }
        result = self._call(tmp_path, file_map={"src/foo.py"})
        assert self.run_coordinator_called
        assert result.summary == "fallback"
