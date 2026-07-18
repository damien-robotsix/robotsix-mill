"""Dedicated tests for the implement coordinator — ImplementResult
typo-absorption validator and run_coordinator argument/prompt paths.

Fills coverage gaps identified in
``tests/agents/test_agents_rework.py`` — does NOT duplicate existing
tests for the happy path, tools, or ValidationResult.decide.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
            level=2,
            name=None,
            retries=2,
            skills=None,
            modules=False,
            repo_dir=None,
            board_id="",
            **_extra,
        ):
            self.captured["system_prompt"] = system_prompt
            self.captured["output_type"] = output_type
            self.captured["tools"] = tools
            self.captured["web_knowledge"] = web_knowledge
            self.captured["name"] = name
            self.captured["level"] = level
            self.captured["repo_dir"] = repo_dir
            self.captured["board_id"] = board_id

            class _FakeAgent:
                @staticmethod
                def run_sync(prompt, *, usage_limits=None, message_history=None):
                    self.captured["usage_limits"] = usage_limits
                    self.captured["message_history"] = message_history
                    self.captured["user_prompt"] = prompt

                    class _R:
                        output: object = ImplementResult(summary="ok")

                        @staticmethod
                        def all_messages():
                            # Return a tool-call message so the
                            # zero-tool-call gate in
                            # reprompt_if_unstructured sees that tools
                            # were used and falls through to the
                            # existing structured-output check.
                            return [
                                SimpleNamespace(
                                    parts=[SimpleNamespace(part_kind="tool-call")]
                                )
                            ]

                    r = _R()
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

        def _fake_build_fs_tools(
            root, settings, *, pre_seeded=None, extra_roots=None, sandbox_image=None
        ):
            self.captured["fs_pre_seeded"] = pre_seeded
            self.captured["fs_extra_roots"] = extra_roots
            self.captured["fs_sandbox_image"] = sandbox_image

            # Return a single read_file tool so the filtering in
            # run_coordinator doesn't blow up.
            def _read_file(path, offset=1, limit=None):
                return "fake content"

            _read_file.__name__ = "read_file"
            return [_read_file]

        monkeypatch.setattr(_fs, "build_fs_tools", _fake_build_fs_tools)

        # ── make_explore_tool (robotsix_mill.agents.explore) ────────
        from robotsix_mill.agents import explore as _expl

        def _fake_make_explore_tool(
            settings, repo_dir, extra_roots=None, pre_seeded_paths=None
        ):
            self.captured["explore_extra_roots"] = extra_roots
            self.captured["explore_pre_seeded_paths"] = pre_seeded_paths

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

    # -- workspace confinement (claude_sdk) ------------------------------

    def test_repo_dir_forwarded_to_builder_for_sdk_confinement(
        self, settings, tmp_path
    ):
        """run_coordinator MUST forward ``repo_dir`` to the agent builder so the
        Claude SDK confines its built-in Edit/Write/Bash to the ticket clone
        (``cwd=repo_dir``). Without it the SDK defaulted to the worker's own
        ``/app`` source tree: edits + test runs hit ``/app``, the clone stayed
        pristine, and the ticket blocked with "no changes produced" while the
        agent reported success (the systemic claude_sdk work-loss)."""
        self._run(settings, tmp_path)
        assert self.captured["repo_dir"] == tmp_path

    def test_board_id_forwarded_to_builder_for_report_issue(self, settings, tmp_path):
        """run_coordinator MUST forward ``board_id`` to the builder so the
        report_issue tool can file a blocker/dependency ticket. Without it the
        tool is built with board_id="" and fails at call time ("board_id is
        required"), so an agent that legitimately cannot proceed surfaces only
        a generic "no changes produced" block."""
        self._run(settings, tmp_path, board_id="robotsix-llmio")
        assert self.captured["board_id"] == "robotsix-llmio"

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
        # The injected directive must match implement.yaml's static
        # guidance: implement only replies; it has no close_thread tool.
        assert "reply_to_thread" in prompt
        assert "close_thread" not in prompt

    # -- delta-context trimming on retry ---------------------------------

    def test_delta_context_on_retry_trims_spec_drops_epic_and_memory(
        self,
        settings,
        tmp_path,
    ):
        """On a retry pass (feedback present), delta_context_retry_enabled
        trims the spec, drops epic context, and drops memory — passing
        only the delta (failure diagnosis + minimal spec reminder)."""
        long_spec = "do X\n\n" + ("padding line\n" * 200)
        self._run(
            settings,
            tmp_path,
            spec=long_spec,
            feedback="test_x failed",
            epic_context="## Epic goal\nepic text",
            memory="board conventions ledger",
        )
        prompt: str = self.captured["user_prompt"]
        # Delta trimming is active by default → spec should be truncated.
        assert "spec truncated" in prompt
        assert "you already read the full spec on the first pass" in prompt
        # Full epic context must NOT appear.
        assert "## Epic goal" not in prompt
        # Full memory must NOT appear.
        assert "board conventions ledger" not in prompt
        # The failure diagnosis (delta) MUST appear.
        assert "test_x failed" in prompt
        assert "test-failure" in prompt

    def test_delta_context_disabled_passes_full_context(
        self,
        settings,
        tmp_path,
    ):
        """When delta_context_retry_enabled is False, retry passes still
        receive the full spec, epic context, and memory."""
        from robotsix_mill.config import Settings

        # Build a settings instance with trimming disabled.
        s = Settings(
            data_dir=str(tmp_path),
            require_approval="false",
            delta_context_retry_enabled=False,
        )
        self._run(
            s,
            tmp_path,
            spec="do X",
            feedback="test_x failed",
            epic_context="## Epic goal\nepic text",
            memory="board conventions ledger",
        )
        prompt: str = self.captured["user_prompt"]
        # Full spec should appear.
        assert "do X" in prompt
        # Epic context should appear.
        assert "## Epic goal" in prompt
        # Memory should appear.
        assert "board conventions ledger" in prompt

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
        for part, path in zip(resp.parts, ("a.py", "b.py", "c.py"), strict=True):
            assert isinstance(part, ToolCallPart)
            assert part.tool_name == "read_file"
            assert part.args == {"path": path, "offset": 1, "limit": None}
            assert part.tool_call_id == f"preload_{path}"

        # Matching ToolReturnParts — same order, same ids.
        for part, (path, content) in zip(
            req.parts,
            [("a.py", "A"), ("b.py", "B"), ("c.py", "C")],
            strict=True,
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

    def test_pre_seeded_paths_forwarded_to_explore(
        self,
        settings,
        tmp_path,
    ):
        """When the coordinator pre-seeds ``reference_files`` it forwards
        the matching relative paths to ``make_explore_tool`` as
        ``pre_seeded_paths`` so the context-isolated scout does NOT re-read
        files the parent already has loaded."""
        (tmp_path / "model.py").write_text("M")
        (tmp_path / "provider.py").write_text("P")
        ref = [{"path": "model.py"}, {"path": "provider.py"}]
        self._run(settings, tmp_path, reference_files=ref)

        assert self.captured["explore_pre_seeded_paths"] == [
            "model.py",
            "provider.py",
        ]

    def test_pre_seeded_paths_none_with_message_history(
        self,
        settings,
        tmp_path,
    ):
        """On the resume path (``message_history`` provided) the parent does
        NOT pre-seed, so ``pre_seeded_paths`` must be ``None`` — never claim
        files are loaded when they are not."""
        (tmp_path / "model.py").write_text("M")
        ref = [{"path": "model.py"}]
        self._run(
            settings,
            tmp_path,
            reference_files=ref,
            message_history=["existing"],
        )

        assert self.captured["explore_pre_seeded_paths"] is None

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

    # -- level -----------------------------------------------------------

    def test_explicit_level_forwarded(self, settings, tmp_path):
        """Explicit ``level`` is passed to ``build_agent``."""
        self._run(
            settings,
            tmp_path,
            level=1,
        )
        assert self.captured["level"] == 1

    def test_level_none_uses_build_agent_default(
        self,
        settings,
        tmp_path,
    ):
        """When ``level`` is None, run_coordinator passes no override and
        build_agent applies its default level (2)."""
        self._run(settings, tmp_path, level=None)
        assert self.captured["level"] == 2

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
        prompt = self.captured["system_prompt"]
        assert prompt.startswith(definition.system_prompt)
        # repo_dir is now in the user prompt (not system prompt) so the
        # static system preamble stays cacheable across calls.
        user_prompt = self.captured["user_prompt"]
        assert (
            f"The repository root (CWD for all run_command calls) is: {tmp_path}"
            in user_prompt
        )

    # -- language_instructions -------------------------------------------

    def test_language_instructions_injected_into_user_prompt(
        self,
        settings,
        tmp_path,
    ):
        """When ``language_instructions`` is non-empty it is prepended
        in the user prompt under a ``## Language conventions`` heading."""
        snippet = "Use pytest. Never run uv sync."
        self._run(settings, tmp_path, language_instructions=snippet)
        prompt: str = self.captured["user_prompt"]
        assert "## Language conventions\n\n" + snippet in prompt
        # The language conventions appear before the ticket-spec.
        conventions_pos = prompt.index("## Language conventions")
        spec_pos = prompt.index("````ticket-spec")
        assert conventions_pos < spec_pos

    def test_language_instructions_empty_unchanged(
        self,
        settings,
        tmp_path,
    ):
        """When ``language_instructions`` is empty (default), the user
        prompt does NOT contain a ``## Language conventions`` block."""
        self._run(settings, tmp_path, language_instructions="")
        prompt = self.captured["user_prompt"]
        # repo_dir is still present.
        assert (
            f"The repository root (CWD for all run_command calls) is: {tmp_path}"
            in prompt
        )
        # No language conventions block when language_instructions is empty.
        assert "## Language conventions" not in prompt

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

    def test_usage_limits_uses_coordinator_max_tool_calls(
        self,
        settings,
        tmp_path,
    ):
        """The ``tool_calls_limit`` on the ``UsageLimits`` passed to
        ``run_sync`` comes from ``settings.coordinator_max_tool_calls``
        (not None — the backstop is always wired)."""
        s = _settings(tmp_path, coordinator_max_tool_calls="42")
        self._run(s, tmp_path)
        ul = self.captured["usage_limits"]
        assert isinstance(ul, UsageLimits)
        assert ul.tool_calls_limit == 42

    def test_usage_limits_tool_calls_limit_is_never_none(
        self,
        settings,
        tmp_path,
    ):
        """Even with default settings, ``tool_calls_limit`` is not None
        — the default 300 from ``LimitsSettings`` is wired through."""
        s = _settings(tmp_path)
        self._run(s, tmp_path)
        ul = self.captured["usage_limits"]
        assert isinstance(ul, UsageLimits)
        assert ul.tool_calls_limit is not None
        assert ul.tool_calls_limit == 300

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
        None, the ``<previous_attempt>`` block IS injected (needed for
        the flash fallback path where the pro model wrote edits but
        produced malformed output)."""
        self._run(
            settings,
            tmp_path,
            previous_attempt_summary="prior summary text",
        )
        prompt: str = self.captured["user_prompt"]
        assert "````previous-attempt" in prompt
        assert "prior summary text" in prompt

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


# ---------------------------------------------------------------------------
# _call_with_timeout — watchdog timeout propagation
# ---------------------------------------------------------------------------


class TestCallWithTimeout:
    """Tests for ``_call_with_timeout`` shutdown behaviour."""

    def test_shutdown_uses_wait_false(self, monkeypatch):
        """_call_with_timeout must call executor.shutdown(wait=False) so
        the watchdog TimeoutError propagates immediately instead of
        blocking on hung threads at executor exit."""
        from robotsix_mill.agents.coordinating import _call_with_timeout

        shutdown_calls = []

        class FakeExecutor:
            def __init__(self, max_workers=1):
                pass

            def submit(self, fn, *args, **kwargs):
                class _Future:
                    def result(self, timeout=None):
                        return fn(*args, **kwargs)

                    def cancel(self):
                        pass

                return _Future()

            def shutdown(self, wait=True):
                shutdown_calls.append(wait)

        import concurrent.futures

        original = concurrent.futures.ThreadPoolExecutor
        concurrent.futures.ThreadPoolExecutor = FakeExecutor
        try:
            result = _call_with_timeout(lambda: 42, timeout_seconds=10)
        finally:
            concurrent.futures.ThreadPoolExecutor = original

        assert result == 42
        assert shutdown_calls == [False], (
            f"Expected shutdown(wait=False), got {shutdown_calls}"
        )

    def test_contextvars_propagated_to_worker_thread(self):
        """contextvars set in the calling thread must be visible inside
        the worker thread — ThreadPoolExecutor drops them by default,
        so _call_with_timeout must propagate via contextvars.copy_context()."""
        import contextvars
        from robotsix_mill.agents.coordinating import _call_with_timeout

        test_var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "test_var", default="default"
        )
        test_var.set("outer")

        captured: list[str | None] = []

        def _read_in_worker():
            captured.append(test_var.get())

        _call_with_timeout(_read_in_worker, timeout_seconds=10)
        assert captured == ["outer"], (
            f"contextvar should be 'outer' in worker, got {captured}"
        )

    def test_timeout_raises_with_shutdown_wait_false(self):
        """When the function times out, _call_with_timeout raises
        TimeoutError and still calls shutdown(wait=False)."""
        import concurrent.futures
        import time

        # Use a real executor but with a function that sleeps past the timeout
        called_shutdown = {"wait": None}

        class InstrumentedExecutor(concurrent.futures.ThreadPoolExecutor):
            def shutdown(self, wait=True):
                called_shutdown["wait"] = wait
                super().shutdown(wait=wait)

        import robotsix_mill.agents.coordinating as _mod

        original_executor = concurrent.futures.ThreadPoolExecutor
        concurrent.futures.ThreadPoolExecutor = InstrumentedExecutor
        try:
            with pytest.raises(TimeoutError):
                _mod._call_with_timeout(
                    lambda: time.sleep(0.2),
                    timeout_seconds=0.05,
                    what="test op",
                )
        finally:
            concurrent.futures.ThreadPoolExecutor = original_executor

        assert called_shutdown["wait"] is False, (
            f"Expected shutdown(wait=False), got {called_shutdown}"
        )


# ---------------------------------------------------------------------------
# implement_pass_timeout — implement stage pass cap
# ---------------------------------------------------------------------------


class TestImplementPassTimeout:
    """Tests for implement_pass_timeout setting."""

    def test_implement_pass_timeout_default(self):
        """implement_pass_timeout defaults to 300 seconds."""
        s = Settings()
        assert s.implement_pass_timeout == 300

    def test_sandbox_op_timeout_default(self):
        """sandbox_op_timeout defaults to 300 seconds."""
        s = Settings()
        assert s.sandbox_op_timeout == 300


# ---------------------------------------------------------------------------
# progress-reset watchdog — _call_with_progress_watchdog
# ---------------------------------------------------------------------------


class TestProgressWatchdog:
    """Tests for _call_with_progress_watchdog and tool wrapping."""

    def test_progress_resets_deadline(self):
        """Each progress_event.set() resets the deadline, allowing a
        long-running function to survive past the initial timeout."""
        import threading
        import time
        from robotsix_mill.agents.coordinating import (
            _call_with_progress_watchdog,
        )

        ev = threading.Event()
        started = threading.Event()
        done = threading.Event()

        def _slow_with_progress():
            started.set()
            # Simulate 4 rounds of "work" with progress between rounds
            for _ in range(4):
                time.sleep(0.15)  # each round > poll_interval
                ev.set()  # signal progress
            done.set()
            return "ok"

        result = _call_with_progress_watchdog(
            _slow_with_progress,
            timeout_seconds=0.25,  # > one round, < total without progress
            progress_event=ev,
            poll_interval=0.05,
        )
        assert result == "ok"
        assert done.is_set()

    def test_no_progress_raises_timeout_error(self):
        """When progress_event is never set, the watchdog raises
        TimeoutError after timeout_seconds elapse."""
        import threading
        import time
        from robotsix_mill.agents.coordinating import (
            _call_with_progress_watchdog,
        )

        ev = threading.Event()

        def _hung():
            time.sleep(1.0)
            return "never"

        with pytest.raises(TimeoutError, match="no progress"):
            _call_with_progress_watchdog(
                _hung,
                timeout_seconds=0.1,
                progress_event=ev,
                poll_interval=0.02,
            )

    def test_contextvars_propagated_to_worker_thread(self):
        """contextvars set in the calling thread must be visible inside
        the worker thread — ThreadPoolExecutor drops them by default,
        so _call_with_progress_watchdog must propagate them."""
        import contextvars
        import threading
        from robotsix_mill.agents.coordinating import (
            _call_with_progress_watchdog,
        )

        test_var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "test_var", default="default"
        )
        test_var.set("outer")

        captured: list[str | None] = []
        ev = threading.Event()
        ev.set()  # ensure no timeout before result

        def _read_in_worker():
            captured.append(test_var.get())

        _call_with_progress_watchdog(
            _read_in_worker,
            timeout_seconds=10,
            progress_event=ev,
        )
        assert captured == ["outer"], (
            f"contextvar should be 'outer' in worker, got {captured}"
        )

    def test_shutdown_uses_wait_false(self):
        """_call_with_progress_watchdog always calls shutdown(wait=False)."""
        import concurrent.futures
        import threading
        from robotsix_mill.agents.coordinating import (
            _call_with_progress_watchdog,
        )

        shutdown_calls: list = []
        _RealFuture = concurrent.futures.Future

        class FakeExecutor:
            def __init__(self, max_workers=1):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, fn, *a, **kw):
                fut = _RealFuture()
                fut.set_result(fn(*a, **kw))
                return fut

            def shutdown(self, wait=True):
                shutdown_calls.append(wait)

        import concurrent.futures as _cf

        original = _cf.ThreadPoolExecutor
        _cf.ThreadPoolExecutor = FakeExecutor
        try:
            ev = threading.Event()
            _call_with_progress_watchdog(
                lambda: 42, timeout_seconds=10, progress_event=ev
            )
        finally:
            _cf.ThreadPoolExecutor = original

        assert shutdown_calls == [False], (
            f"Expected shutdown(wait=False), got {shutdown_calls}"
        )

    def test_wrap_tools_signals_progress_event(self):
        """_wrap_tools_with_progress wraps callables so they set the
        progress_event on every invocation."""
        import threading
        from robotsix_mill.agents.coordinating import (
            _wrap_tools_with_progress,
        )

        ev = threading.Event()
        called = []

        def orig_tool(x):
            called.append(x)
            return x * 2

        wrapped = _wrap_tools_with_progress([orig_tool], ev)
        assert len(wrapped) == 1

        result = wrapped[0](3)
        assert result == 6
        assert called == [3]
        assert ev.is_set(), "progress_event must be set after tool call"

    async def test_wrap_tools_keeps_async_tools_async(self):
        """An async tool stays a coroutine function after wrapping and its
        awaited result is the tool's return value, not a bare coroutine."""
        import inspect
        import threading
        from robotsix_mill.agents.coordinating import (
            _wrap_tools_with_progress,
        )

        ev = threading.Event()
        called = []

        async def orig_tool(x):
            called.append(x)
            return x * 2

        wrapped = _wrap_tools_with_progress([orig_tool], ev)
        assert inspect.iscoroutinefunction(wrapped[0]), (
            "async tools must remain coroutine functions after wrapping"
        )

        result = await wrapped[0](3)
        assert result == 6
        assert called == [3]
        assert ev.is_set(), "progress_event must be set after tool call"

    def test_wrap_tools_skips_non_callables(self):
        """Non-callable entries pass through _wrap_tools_with_progress
        unchanged."""
        import threading
        from robotsix_mill.agents.coordinating import (
            _wrap_tools_with_progress,
        )

        ev = threading.Event()
        obj = object()
        wrapped = _wrap_tools_with_progress([obj], ev)
        assert wrapped[0] is obj
