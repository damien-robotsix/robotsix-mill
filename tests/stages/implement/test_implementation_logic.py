"""Unit tests for ImplementationLogicMixin classmethods.

Exercises ``_select_agent_level``, ``_invoke_implement_agent``,
``_evaluate_test_results``, and ``_persist_pass_artifacts`` in
isolation with all heavy collaborators mocked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from robotsix_mill.agents.coding import AgentBudgetError, AgentRunError
from robotsix_mill.core.states import State
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.implement._shared import (
    _ImplementContext,
    _SinglePassResult,
)
from robotsix_mill.stages.implement.core import ImplementStage
from robotsix_mill.stages.implement.implementation_logic import (
    _verify_summary_claims,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ic(**overrides) -> _ImplementContext:
    defaults: dict = {
        "spec": "Add feature",
        "memory_text": "",
        "reference_files": None,
        "file_map": None,
        "feedback": None,
        "previous_attempt_summary": None,
        "open_thread_ids": None,
    }
    defaults.update(overrides)
    return _ImplementContext(**defaults)


def _simple_namespace(**kw):
    defaults = {"max_fix_iterations": 3}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


# The assembled ``ImplementStage`` class has all mixin methods resolved
# via MRO.  We invoke classmethods on it (not on
# ``ImplementationLogicMixin`` directly) because the real call site
# is always ``cls.<method>(...)`` where ``cls`` is the leaf assembled
# class, and cross-mixin calls like ``cls._finalize(...)`` resolve
# only when the full MRO is present.
_Stage = ImplementStage

# ---------------------------------------------------------------------------
# 1. _select_agent_level — pure logic, no sibling calls.
# ---------------------------------------------------------------------------


class TestSelectAgentLevel:
    @staticmethod
    def _call(ic, settings=None, repo_dir=None, target_branch="main"):
        if settings is None:
            settings = _simple_namespace()
        if repo_dir is None:
            repo_dir = Path("/fake/repo")
        return _Stage._select_agent_level(ic, settings, repo_dir, target_branch)

    def test_no_change_in_summary(self):
        ic = _ic(previous_attempt_summary="no change needed after inspection")
        result = self._call(ic)
        assert result == 1

    def test_no_change_in_feedback(self):
        ic = _ic(feedback="NO CHANGE NEEDED — already satisfied")
        result = self._call(ic)
        assert result == 1

    def test_no_change_in_both_fields(self):
        ic = _ic(
            previous_attempt_summary="summary text",
            feedback="feedback with No Change Needed here",
        )
        result = self._call(ic)
        assert result == 1

    def test_no_phrase_returns_none(self):
        ic = _ic(
            previous_attempt_summary="everything looks fine",
            feedback="tests pass",
        )
        result = self._call(ic)
        assert result is None

    def test_none_fields_returns_none(self):
        ic = _ic(previous_attempt_summary=None, feedback=None)
        result = self._call(ic)
        assert result is None

    def test_empty_strings_returns_none(self):
        ic = _ic(previous_attempt_summary="", feedback="")
        result = self._call(ic)
        assert result is None

    def test_trivial_config_bypass_suppressed_by_reviewer_feedback(
        self, tmp_path, monkeypatch
    ):
        """A reviewer sendback must not take the deterministic no-LLM path.

        Regression (2026-07-31): level -2 applies whatever is already in the
        working tree without reading ``feedback``, so on a review sendback it
        re-emitted the exact diff the reviewer had rejected. Review sent it
        back, implement bypassed again, and after four identical passes the
        cycle ceiling blocked the ticket — four tickets on the live board,
        each with a review naming a concrete edit and four passes summarised
        "trivial config-only addition: 1 new file(s)".
        """
        import robotsix_mill.stages.implement.implementation_logic as mod

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        monkeypatch.setattr(mod, "_is_trivial_config_only_change", lambda *a, **k: True)
        monkeypatch.setattr(mod, "_is_config_only_change", lambda *a, **k: False)
        monkeypatch.setattr(mod, "_is_rename_only_change", lambda *a, **k: False)
        monkeypatch.setattr(mod, "_is_spec_exact_edits", lambda *a, **k: False)

        # No feedback → the cheap deterministic path is still taken.
        assert self._call(_ic(), repo_dir=repo_dir) == -2

        # With reviewer feedback → must reach an LLM level instead.
        ic = _ic(feedback="Remove the docs/modules.yaml line; it contradicts the rule.")
        assert self._call(ic, repo_dir=repo_dir) != -2

    @pytest.mark.parametrize(
        ("detector", "bypass_level"),
        [
            ("_is_rename_only_change", 0),
            ("_is_spec_exact_edits", -1),
        ],
    )
    def test_other_llm_bypasses_suppressed_by_feedback(
        self, tmp_path, monkeypatch, detector, bypass_level
    ):
        """The rename-only and spec-exact bypasses are equally LLM-free."""
        import robotsix_mill.stages.implement.implementation_logic as mod

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        for name in (
            "_is_trivial_config_only_change",
            "_is_config_only_change",
            "_is_rename_only_change",
            "_is_spec_exact_edits",
        ):
            monkeypatch.setattr(mod, name, lambda *a, **k: False)
        monkeypatch.setattr(mod, detector, lambda *a, **k: True)

        assert self._call(_ic(), repo_dir=repo_dir) == bypass_level

        ic = _ic(feedback="Please address the two points above.")
        assert self._call(ic, repo_dir=repo_dir) != bypass_level

    def test_config_only_level_1_still_allowed_with_feedback(
        self, tmp_path, monkeypatch
    ):
        """Level 1 is a real LLM pass, so feedback must not suppress it."""
        import robotsix_mill.stages.implement.implementation_logic as mod

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        monkeypatch.setattr(
            mod, "_is_trivial_config_only_change", lambda *a, **k: False
        )
        monkeypatch.setattr(mod, "_is_config_only_change", lambda *a, **k: True)
        monkeypatch.setattr(mod, "_is_rename_only_change", lambda *a, **k: False)
        monkeypatch.setattr(mod, "_is_spec_exact_edits", lambda *a, **k: False)

        ic = _ic(feedback="Complete the truncated sentence in the newsfragment.")
        assert self._call(ic, repo_dir=repo_dir) == 1

    def test_config_only_change_returns_level_1(self, tmp_path, monkeypatch):
        """A ticket whose diff is all .md/.yaml gets level-1."""
        import subprocess

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        # Simulate git diff returning only config-only files.
        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="docs/readme.md\nconfig/settings.yaml\n"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        ic = _ic()
        result = self._call(ic, repo_dir=repo_dir)
        assert result == 1

    def test_py_file_in_diff_returns_none(self, tmp_path, monkeypatch):
        """A ticket with a .py change still gets level-2 (None)."""
        import subprocess

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="src/module.py\ndocs/readme.md\n"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        ic = _ic()
        result = self._call(ic, repo_dir=repo_dir)
        assert result is None

    def test_config_only_change_with_no_change_needed_returns_level_1(
        self, tmp_path, monkeypatch
    ):
        """When both heuristics fire, config-only + no-change-needed still returns 1."""
        import subprocess

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="docs/readme.md\n"
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        ic = _ic(previous_attempt_summary="no change needed after inspection")
        result = self._call(ic, repo_dir=repo_dir)
        assert result == 1

    def test_spec_exact_edits_returns_neg1(self, tmp_path):
        """A spec with fenced code blocks referencing real files returns -1."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "module.py").write_text("# existing")

        spec = """### `src/module.py`

```python
# new code
```
"""
        ic = _ic(spec=spec)
        result = self._call(ic, repo_dir=repo_dir)
        assert result == -1

    def test_spec_exact_edits_missing_file_returns_none(self, tmp_path):
        """When a referenced file doesn't exist, fall through to level-2 (None)."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        # src/missing.py does NOT exist.

        spec = """### `src/missing.py`

```python
# code
```
"""
        ic = _ic(spec=spec)
        result = self._call(ic, repo_dir=repo_dir)
        assert result is None

    def test_spec_exact_no_code_blocks_returns_none(self):
        """A spec without code blocks returns None."""
        ic = _ic(spec="Just prose, no code blocks.")
        result = self._call(ic)
        assert result is None


# ---------------------------------------------------------------------------
# 2. _invoke_implement_agent
# ---------------------------------------------------------------------------


class FakeTicket:
    id = "test-ticket-1"
    board_id = "test-board"
    implement_cycles = 0


def _stage_ctx(**kw):
    defaults = {"repo_config": None}
    defaults.update(kw)
    return SimpleNamespace(**defaults)


_DUMMY_PATH = Path("/fake/repo")


class TestInvokeImplementAgent:
    def test_success_path(self, monkeypatch):
        """run_implement_agent returns a 7-tuple → _AgentRunOutcome.success."""
        fake_result = ("summary", ["f.py"], "memory", b"cs", b"ms", True, "rationale")
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: fake_result,
        )

        outcome = _Stage._invoke_implement_agent(
            ctx=_stage_ctx(),
            ticket=FakeTicket(),
            repo_dir=_DUMMY_PATH,
            branch="main",
            settings=_simple_namespace(),
            ic=_ic(),
            language_instructions="",
            agent_level=None,
            resume_history=None,
            extra_roots=None,
            memory_board_id="mb",
        )
        assert outcome.success == fake_result
        assert outcome.failure is None

    def test_budget_error(self, monkeypatch):
        """AgentBudgetError → failure with BLOCKED, calls _finalize(ok=False)."""
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(AgentBudgetError("cap hit", [])),
        )

        finalize_calls = []

        def _fake_finalize(cls, ctx, ticket, repo_dir, branch, summary, *, ok, **kw):
            finalize_calls.append({"ok": ok, "summary": summary})

        monkeypatch.setattr(_Stage, "_finalize", classmethod(_fake_finalize))

        outcome = _Stage._invoke_implement_agent(
            ctx=_stage_ctx(),
            ticket=FakeTicket(),
            repo_dir=_DUMMY_PATH,
            branch="main",
            settings=_simple_namespace(),
            ic=_ic(),
            language_instructions="",
            agent_level=None,
            resume_history=None,
            extra_roots=None,
            memory_board_id="mb",
        )
        assert outcome.success is None
        assert outcome.failure is not None
        assert outcome.failure.next_action == "return"
        assert outcome.failure.outcome.next_state is State.BLOCKED
        assert "budget" in outcome.failure.outcome.note.lower()
        assert len(finalize_calls) == 1
        assert finalize_calls[0]["ok"] is False

    def test_budget_error_saves_conversation_state(self, monkeypatch, tmp_path):
        """AgentBudgetError with conversation_state + ws → saves state."""
        fake_conv_state = b'{"messages": ["test"]}'
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(
                AgentBudgetError("cap hit", [], conversation_state=fake_conv_state)
            ),
        )

        finalize_calls = []

        def _fake_finalize(cls, ctx, ticket, repo_dir, branch, summary, *, ok, **kw):
            finalize_calls.append({"ok": ok, "summary": summary})

        monkeypatch.setattr(_Stage, "_finalize", classmethod(_fake_finalize))

        # Create a fake workspace with an artifacts_dir so
        # save_conversation_state can write the file.
        fake_ws = SimpleNamespace(artifacts_dir=tmp_path)

        outcome = _Stage._invoke_implement_agent(
            ctx=_stage_ctx(),
            ticket=FakeTicket(),
            repo_dir=_DUMMY_PATH,
            branch="main",
            settings=_simple_namespace(),
            ic=_ic(),
            language_instructions="",
            agent_level=None,
            resume_history=None,
            extra_roots=None,
            memory_board_id="mb",
            ws=fake_ws,
        )
        assert outcome.success is None
        assert outcome.failure is not None
        assert outcome.failure.next_action == "return"
        assert len(finalize_calls) == 1

        # Verify the conversation state file was written.
        state_path = tmp_path / "implement_conversation_state.json"
        assert state_path.exists()
        assert state_path.read_bytes() == fake_conv_state

    def test_budget_error_no_ws_skips_save(self, monkeypatch):
        """AgentBudgetError with conversation_state but ws=None → no save."""
        fake_conv_state = b'{"messages": ["test"]}'
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(
                AgentBudgetError("cap hit", [], conversation_state=fake_conv_state)
            ),
        )

        finalize_calls = []

        def _fake_finalize(cls, ctx, ticket, repo_dir, branch, summary, *, ok, **kw):
            finalize_calls.append({"ok": ok, "summary": summary})

        monkeypatch.setattr(_Stage, "_finalize", classmethod(_fake_finalize))

        outcome = _Stage._invoke_implement_agent(
            ctx=_stage_ctx(),
            ticket=FakeTicket(),
            repo_dir=_DUMMY_PATH,
            branch="main",
            settings=_simple_namespace(),
            ic=_ic(),
            language_instructions="",
            agent_level=None,
            resume_history=None,
            extra_roots=None,
            memory_board_id="mb",
            # ws defaults to None — save_conversation_state must not be called
        )
        assert outcome.success is None
        assert outcome.failure is not None
        assert outcome.failure.next_action == "return"
        assert len(finalize_calls) == 1

    def test_agent_error_non_transient(self, monkeypatch):
        """AgentRunError with non-transient cause → failure BLOCKED, no re-raise."""
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(
                AgentRunError("boom", [], cause=ValueError("x"))
            ),
        )
        # 2-arg form so monkeypatch resolves the dotted path correctly.
        monkeypatch.setattr(
            "robotsix_mill.runtime.transient_errors.classify_stage_error",
            lambda exc: "fatal",
        )
        monkeypatch.setattr(_Stage, "_finalize", lambda *a, **kw: None)

        outcome = _Stage._invoke_implement_agent(
            ctx=_stage_ctx(),
            ticket=FakeTicket(),
            repo_dir=_DUMMY_PATH,
            branch="main",
            settings=_simple_namespace(),
            ic=_ic(),
            language_instructions="",
            agent_level=None,
            resume_history=None,
            extra_roots=None,
            memory_board_id="mb",
        )
        assert outcome.success is None
        assert outcome.failure is not None
        assert outcome.failure.next_action == "return"
        assert outcome.failure.outcome.next_state is State.BLOCKED
        assert "agent error" in outcome.failure.outcome.note.lower()

    @staticmethod
    def _run_agent_error(monkeypatch, err):
        """Drive ``_invoke_implement_agent`` into its AgentRunError handler
        and capture the ``_finalize`` kwargs it used."""
        finalize_calls: list[dict] = []
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(err),
        )
        monkeypatch.setattr(
            _Stage,
            "_finalize",
            lambda *a, **kw: finalize_calls.append(kw),
        )
        outcome = _Stage._invoke_implement_agent(
            ctx=_stage_ctx(),
            ticket=FakeTicket(),
            repo_dir=_DUMMY_PATH,
            branch="main",
            settings=_simple_namespace(),
            ic=_ic(),
            language_instructions="",
            agent_level=None,
            resume_history=None,
            extra_roots=None,
            memory_board_id="mb",
        )
        return outcome, finalize_calls

    def test_provider_failure_without_cause_records_no_fingerprint(self, monkeypatch):
        """The live cd92 shape: dual primary+fallback failure raised with NO
        typed cause.  It must block but must NOT be treated as
        spec-determined — ``_finalize(transient=True)`` skips the spec
        fingerprint so resume-blocked actually re-runs the attempt."""
        err = AgentRunError(
            "output retries exhausted on primary + fallback models: "
            "primary=Model token limit (32768) exceeded before any response "
            "was generated., fallback=status_code: 400, model_name: "
            "deepseek/deepseek-v4-flash-latest, body: {'message': "
            "'deepseek/deepseek-v4-flash-latest is not a valid model ID'}",
            [],
        )
        outcome, finalize_calls = self._run_agent_error(monkeypatch, err)

        assert outcome.failure is not None
        assert outcome.failure.outcome.next_state is State.BLOCKED
        assert "no spec fingerprint recorded" in outcome.failure.outcome.note
        assert len(finalize_calls) == 1
        assert finalize_calls[0]["ok"] is False
        assert finalize_calls[0]["transient"] is True

    def test_provider_failure_via_typed_cause_records_no_fingerprint(self, monkeypatch):
        err = AgentRunError(
            "agent error",
            [],
            cause=RuntimeError("deepseek/x is not a valid model ID"),
        )
        outcome, finalize_calls = self._run_agent_error(monkeypatch, err)
        assert outcome.failure.outcome.next_state is State.BLOCKED
        assert finalize_calls[0]["transient"] is True

    def test_agent_behaviour_failure_without_cause_stays_spec_determined(
        self, monkeypatch
    ):
        """An agent that loops on tool validation IS a verdict on this spec:
        the fingerprint guard must still arm (no regression)."""
        err = AgentRunError("Tool 'verify_diff' exceeded max retries count of 2", [])
        outcome, finalize_calls = self._run_agent_error(monkeypatch, err)
        assert outcome.failure.outcome.next_state is State.BLOCKED
        assert "no spec fingerprint" not in outcome.failure.outcome.note
        assert finalize_calls[0]["transient"] is False

    def test_transient_message_without_cause_re_raises(self, monkeypatch):
        """No typed cause, but the message itself is a known transient
        pattern → re-raise for the worker's retry-with-backoff, and never
        write implement.md."""
        err = AgentRunError(
            "Invalid response from openrouter chat completions endpoint", []
        )
        with pytest.raises(AgentRunError):
            self._run_agent_error(monkeypatch, err)

    def test_claude_sdk_transport_failure_without_cause_re_raises_no_fingerprint(
        self, monkeypatch
    ):
        """Claude Agent SDK transport/process failure (ClaudeSDKAPIError) is
        infrastructure, not a verdict on the spec: the attempt must re-raise
        for the worker's retry-with-backoff and record NO spec fingerprint.
        Otherwise the next resume hits a false 'spec unchanged' re-block and
        pins the ticket until an operator override (2026-09-01:
        ...-071238Z / ...-073557Z)."""
        err = AgentRunError(
            "Claude Agent SDK transport/process failure (implement): "
            "Claude Code returned an error result: success",
            [],
        )
        with pytest.raises(AgentRunError):
            self._run_agent_error(monkeypatch, err)

    def test_claude_sdk_transport_failure_via_typed_cause_re_raises(self, monkeypatch):
        """Same failure carried as a typed ClaudeSDKAPIError-like cause: the
        handler re-raises the original cause (no fingerprint)."""
        original_cause = RuntimeError(
            "Claude Agent SDK transport/process failure (implement): "
            "Claude Code returned an error result: success"
        )
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(
                AgentRunError("boom", [], cause=original_cause)
            ),
        )
        monkeypatch.setattr(_Stage, "_finalize", lambda *a, **kw: None)

        with pytest.raises(RuntimeError, match="transport/process failure"):
            _Stage._invoke_implement_agent(
                ctx=_stage_ctx(),
                ticket=FakeTicket(),
                repo_dir=_DUMMY_PATH,
                branch="main",
                settings=_simple_namespace(),
                ic=_ic(),
                language_instructions="",
                agent_level=None,
                resume_history=None,
                extra_roots=None,
                memory_board_id="mb",
            )

    def test_agent_error_transient_cause_re_raises(self, monkeypatch):
        """AgentRunError with transient cause re-raises the original cause."""
        original_cause = ConnectionError("timeout")
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(
                AgentRunError("boom", [], cause=original_cause)
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.runtime.transient_errors.classify_stage_error",
            lambda exc: "transient",
        )
        monkeypatch.setattr(_Stage, "_finalize", lambda *a, **kw: None)

        with pytest.raises(ConnectionError, match="timeout"):
            _Stage._invoke_implement_agent(
                ctx=_stage_ctx(),
                ticket=FakeTicket(),
                repo_dir=_DUMMY_PATH,
                branch="main",
                settings=_simple_namespace(),
                ic=_ic(),
                language_instructions="",
                agent_level=None,
                resume_history=None,
                extra_roots=None,
                memory_board_id="mb",
            )

    def test_agent_error_cause_is_none(self, monkeypatch):
        """AgentRunError with cause=None → failure BLOCKED (no re-raise)."""
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(AgentRunError("boom", [], cause=None)),
        )
        monkeypatch.setattr(_Stage, "_finalize", lambda *a, **kw: None)

        outcome = _Stage._invoke_implement_agent(
            ctx=_stage_ctx(),
            ticket=FakeTicket(),
            repo_dir=_DUMMY_PATH,
            branch="main",
            settings=_simple_namespace(),
            ic=_ic(),
            language_instructions="",
            agent_level=None,
            resume_history=None,
            extra_roots=None,
            memory_board_id="mb",
        )
        assert outcome.success is None
        assert outcome.failure is not None
        assert outcome.failure.next_action == "return"
        assert outcome.failure.outcome.next_state is State.BLOCKED


# ---------------------------------------------------------------------------
# 3. _evaluate_test_results
# ---------------------------------------------------------------------------


class TestEvaluateTestResults:
    @staticmethod
    def _install_default_patches(monkeypatch):
        """Install common seam stubs for _evaluate_test_results tests."""
        monkeypatch.setattr(_Stage, "_finalize", lambda *a, **kw: None)
        monkeypatch.setattr(
            _Stage,
            "_any_repo_has_changes",
            lambda *a, **kw: True,
        )
        monkeypatch.setattr(
            _Stage,
            "_claimed_gitignored_edits",
            lambda *a, **kw: [],
        )

        from robotsix_mill.stages import implement as _facade

        monkeypatch.setattr(_facade, "run_test_agent", lambda **kw: (True, ""))
        monkeypatch.setattr(_facade, "run_smoke_agent", lambda **kw: (True, ""))
        monkeypatch.setattr(_facade, "load_repo_smoke_paths", lambda rd: [])
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.load_repo_smoke_command",
            lambda rd: "",
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.target_branch_for",
            lambda s, rc: "main",
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.smoke_paths_match",
            lambda changed, paths: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.short_circuit_verify",
            _simple_namespace(
                detect_edit_claim_contradiction=lambda **kw: [],
                detect_missing_claimed_files=lambda **kw: [],
                cited_fix_unverified=lambda *a, **kw: None,
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.git_ops",
            _simple_namespace(
                introduced_files=lambda rd, tgt: [], has_changes=lambda rd: False
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.acknowledge_unanswered_threads",
            lambda *a: None,
        )
        # _verify_repo_changes moved to implementation_editing.py;
        # patch its direct imports too.
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_editing.short_circuit_verify",
            _simple_namespace(
                detect_missing_claimed_files=lambda **kw: [],
                analyze_pass_progress=lambda new_msgs: {"total": 1},
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_editing.git_ops",
            _simple_namespace(
                introduced_files=lambda rd, tgt: [],
                has_changes=lambda rd: False,
                head_sha=lambda rd: "abc1234",
            ),
        )

    @staticmethod
    def _call(monkeypatch, **overrides):
        """Call _evaluate_test_results with common defaults + overrides.

        IMPORTANT: callers must invoke ``_install_default_patches(monkeypatch)``
        BEFORE any test-specific monkeypatch overrides, THEN call this method.
        """
        params: dict = {
            "ctx": _simple_namespace(
                repo_config=None,
                service=_simple_namespace(
                    add_step_event=lambda *a: None,
                    set_implement_cycles=lambda *a: None,
                ),
            ),
            "ticket": FakeTicket(),
            "repo_dir": _DUMMY_PATH,
            "branch": "main",
            "settings": _simple_namespace(
                review_enabled=True,
                smoke_command="",
            ),
            "ic": _ic(),
            "new_ic": _ic(),
            "summary": "did work",
            "ref_files": None,
            "new_msgs": None,
            "no_change_needed": False,
            "no_change_rationale": "",
            "resuming": False,
            "attempt": 1,
            "max_iters": 3,
            "extra_roots": None,
        }
        params.update(overrides)
        return _Stage._evaluate_test_results(**params)

    def test_proceed_review_enabled(self, monkeypatch):
        """passed=True, has_changes=True, review_enabled=True → CODE_REVIEW."""
        self._install_default_patches(monkeypatch)
        result = self._call(monkeypatch)
        assert result.next_action == "proceed"
        assert result.outcome.next_state is State.CODE_REVIEW

    def test_proceed_documenting_when_review_disabled(self, monkeypatch):
        """passed=True, has_changes=True, review_enabled=False → DOCUMENTING."""
        self._install_default_patches(monkeypatch)
        result = self._call(
            monkeypatch,
            settings=_simple_namespace(review_enabled=False, smoke_command=""),
        )
        assert result.next_action == "proceed"
        assert result.outcome.next_state is State.DOCUMENTING

    def test_escalate_on_exhausted_iterations(self, monkeypatch):
        """failed test on last attempt → escalate BLOCKED."""
        self._install_default_patches(monkeypatch)
        finalize_ok = []

        def _fake_finalize(cls, ctx, ticket, repo_dir, branch, summary, *, ok, **kw):
            finalize_ok.append(ok)

        monkeypatch.setattr(_Stage, "_finalize", classmethod(_fake_finalize))
        from robotsix_mill.stages import implement as _facade

        monkeypatch.setattr(
            _facade, "run_test_agent", lambda **kw: (False, "tests fail")
        )

        result = self._call(monkeypatch, attempt=3, max_iters=3)
        assert result.next_action == "escalate"
        assert result.outcome.next_state is State.BLOCKED
        assert "still failing" in result.outcome.note
        assert finalize_ok == [False]

    def test_retry_while_iterations_remain(self, monkeypatch):
        """failed test with attempt < max_iters → retry with feedback."""
        self._install_default_patches(monkeypatch)
        from robotsix_mill.stages import implement as _facade

        monkeypatch.setattr(
            _facade, "run_test_agent", lambda **kw: (False, "test diag")
        )

        result = self._call(monkeypatch, attempt=1, max_iters=3)
        assert result.next_action == "retry"
        assert result.feedback == "test diag"
        assert result.ic is not None

    def test_sandbox_unavailable_early_return(self, monkeypatch):
        """sandbox unavailable → return BLOCKED immediately."""
        self._install_default_patches(monkeypatch)
        finalize_ok = []

        def _fake_finalize(cls, ctx, ticket, repo_dir, branch, summary, *, ok, **kw):
            finalize_ok.append(ok)

        monkeypatch.setattr(_Stage, "_finalize", classmethod(_fake_finalize))
        from robotsix_mill.stages import implement as _facade

        monkeypatch.setattr(
            _facade,
            "run_test_agent",
            lambda **kw: (False, "sandbox unavailable: no capacity"),
        )

        result = self._call(monkeypatch)
        assert result.next_action == "return"
        assert result.outcome.next_state is State.BLOCKED
        assert "sandbox unavailable" in result.outcome.note
        assert finalize_ok == [False]

    def test_no_change_needed_to_done(self, monkeypatch):
        """no_change_needed + no_changes + no edit tools → DONE."""
        self._install_default_patches(monkeypatch)
        monkeypatch.setattr(
            _Stage,
            "_any_repo_has_changes",
            lambda *a, **kw: False,
        )

        result = self._call(
            monkeypatch,
            no_change_needed=True,
            no_change_rationale="already satisfied",
        )
        assert result.next_action == "return"
        assert result.outcome.next_state is State.DONE
        assert result.outcome.note.startswith("no change needed")

    def test_edit_claim_contradiction_blocks(self, monkeypatch):
        """no_change_needed but edit tools were invoked → BLOCKED."""
        self._install_default_patches(monkeypatch)
        monkeypatch.setattr(
            _Stage,
            "_any_repo_has_changes",
            lambda *a, **kw: False,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.short_circuit_verify",
            _simple_namespace(
                detect_edit_claim_contradiction=lambda **kw: [
                    "write_file",
                    "edit_file",
                ],
                detect_missing_claimed_files=lambda **kw: [],
                cited_fix_unverified=lambda *a, **kw: None,
            ),
        )

        result = self._call(
            monkeypatch,
            no_change_needed=True,
            no_change_rationale="already satisfied",
        )
        assert result.next_action == "return"
        assert result.outcome.next_state is State.BLOCKED
        assert "edit-claim contradiction" in result.outcome.note.lower()

    def test_spec_mandate_blocks_no_change_needed_done(self, monkeypatch):
        """no_change_needed + spec demanding a non-empty diff → BLOCKED."""
        self._install_default_patches(monkeypatch)
        monkeypatch.setattr(
            _Stage,
            "_any_repo_has_changes",
            lambda *a, **kw: False,
        )

        result = self._call(
            monkeypatch,
            ic=_ic(
                spec=(
                    "## Acceptance criteria\n\n"
                    "- The implementation must produce a non-empty diff.\n"
                    "- The ticket must not be closed without changes."
                )
            ),
            no_change_needed=True,
            no_change_rationale="already satisfied",
        )
        assert result.next_action == "return"
        assert result.outcome.next_state is State.BLOCKED
        assert "spec demands code change" in result.outcome.note

    def test_spec_mandate_blocks_fresh_run_empty_diff_done(self, monkeypatch):
        """Empty diff on a fresh run + spec demanding a diff → BLOCKED."""
        self._install_default_patches(monkeypatch)
        monkeypatch.setattr(
            _Stage,
            "_any_repo_has_changes",
            lambda *a, **kw: False,
        )

        result = self._call(
            monkeypatch,
            ic=_ic(spec="This ticket must produce a non-empty diff."),
            no_change_needed=False,
            no_change_rationale="",
            resuming=False,
        )
        assert result.next_action == "return"
        assert result.outcome.next_state is State.BLOCKED
        assert "spec demands code change" in result.outcome.note

    def test_spec_mandate_blocks_resume_empty_diff_done(self, monkeypatch):
        """Empty diff on a resuming run + spec demanding a diff → BLOCKED."""
        self._install_default_patches(monkeypatch)
        monkeypatch.setattr(
            _Stage,
            "_any_repo_has_changes",
            lambda *a, **kw: False,
        )

        result = self._call(
            monkeypatch,
            ic=_ic(spec="The fix must create the following files."),
            no_change_needed=False,
            no_change_rationale="",
            resuming=True,
        )
        assert result.next_action == "return"
        assert result.outcome.next_state is State.BLOCKED
        assert "spec demands code change" in result.outcome.note

    def test_spec_without_mandate_keeps_no_change_done(self, monkeypatch):
        """Spec with a bare 'must' but no mandate phrase → DONE unchanged."""
        self._install_default_patches(monkeypatch)
        monkeypatch.setattr(
            _Stage,
            "_any_repo_has_changes",
            lambda *a, **kw: False,
        )

        result = self._call(
            monkeypatch,
            ic=_ic(spec="The agent must follow the repo style guide when fixing."),
            no_change_needed=True,
            no_change_rationale="already satisfied",
        )
        assert result.next_action == "return"
        assert result.outcome.next_state is State.DONE
        assert result.outcome.note.startswith("no change needed")

    def test_multi_repo_introduced_files_resolves_per_repo_target(
        self, monkeypatch, tmp_path
    ):
        """Each extra_roots repo gets its own target branch from
        target_branch_for, not the primary repo's target."""
        self._install_default_patches(monkeypatch)

        # Create synthetic repo paths whose .name acts as repo_id.
        repo_a = tmp_path / "repos" / "repo-a"
        repo_a.mkdir(parents=True)
        repo_b = tmp_path / "repos" / "repo-b"
        repo_b.mkdir(parents=True)

        # Track the (repo_path, target_branch) pairs passed to introduced_files.
        calls = []

        def _fake_introduced_files(repo_path, tgt):
            calls.append((repo_path, tgt))
            return []

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.git_ops",
            _simple_namespace(
                introduced_files=_fake_introduced_files, has_changes=lambda rd: False
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_editing.git_ops",
            _simple_namespace(
                introduced_files=_fake_introduced_files,
                has_changes=lambda rd: False,
                head_sha=lambda rd: "abc1234",
            ),
        )

        # Per-repo target: repo-a → "custom", repo-b → "develop".
        def _fake_target_branch_for(settings, rc):
            if rc is not None and rc.working_branch:
                return rc.working_branch
            return "main"

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.target_branch_for",
            _fake_target_branch_for,
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_editing.target_branch_for",
            _fake_target_branch_for,
        )

        # get_repo_config returns a fake RepoConfig with working_branch set.
        class _FakeRepoConfig:
            def __init__(self, working_branch):
                self.working_branch = working_branch

        _configs = {
            "repo-a": _FakeRepoConfig("custom"),
            "repo-b": _FakeRepoConfig("develop"),
        }

        def _fake_get_repo_config(repo_id):
            return _configs[repo_id]

        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_editing.get_repo_config",
            _fake_get_repo_config,
        )

        # repo_dir is not in extra_roots, so it's only called once.
        result = self._call(
            monkeypatch,
            repo_dir=repo_a,
            extra_roots=[repo_a, repo_b],
        )
        assert result.next_action == "proceed"

        # repo_a (primary) should be called with primary target (via ctx.repo_config=None → "main")
        # repo_b should be called with its own "develop" target.
        repo_b_calls = [c for c in calls if c[0] == repo_b]
        assert len(repo_b_calls) == 1, f"expected 1 call for repo_b, got {calls}"
        assert repo_b_calls[0][1] == "develop", (
            f"repo_b should get target 'develop', got {repo_b_calls[0][1]}"
        )


# ---------------------------------------------------------------------------
# 4. _persist_pass_artifacts
# ---------------------------------------------------------------------------


class TestPersistPassArtifacts:
    def test_persist_memory_called_when_non_empty(self, monkeypatch, tmp_path):
        """persist_memory is called when updated_memory is non-empty."""
        persist_calls = []
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.persist_memory",
            lambda path, text: persist_calls.append((str(path), text)),
        )

        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        ws = _simple_namespace(artifacts_dir=artifacts_dir)
        settings = _simple_namespace(
            memory_file_for=lambda stage, bid: Path(f"/fake/{bid}_{stage}.md")
        )

        _Stage._persist_pass_artifacts(
            ws=ws,
            ticket=FakeTicket(),
            ic=_ic(),
            summary="did work",
            ref_files=None,
            updated_memory="some memory text",
            settings=settings,
            memory_board_id="mb",
        )
        assert len(persist_calls) == 1
        assert persist_calls[0][1] == "some memory text"
        assert "mb_implement.md" in persist_calls[0][0]

    def test_persist_memory_not_called_when_empty(self, monkeypatch, tmp_path):
        """persist_memory is NOT called when updated_memory is empty."""
        persist_calls = []
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.persist_memory",
            lambda path, text: persist_calls.append((str(path), text)),
        )

        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        ws = _simple_namespace(artifacts_dir=artifacts_dir)
        settings = _simple_namespace(
            memory_file_for=lambda stage, bid: Path(f"/fake/{bid}_{stage}.md")
        )

        _Stage._persist_pass_artifacts(
            ws=ws,
            ticket=FakeTicket(),
            ic=_ic(),
            summary="did work",
            ref_files=None,
            updated_memory="",
            settings=settings,
            memory_board_id="mb",
        )
        assert len(persist_calls) == 0

    def test_reference_files_written(self, monkeypatch, tmp_path):
        """reference_files.json is written under artifacts_dir."""
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.persist_memory",
            lambda path, text: None,
        )

        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        ws = _simple_namespace(artifacts_dir=artifacts_dir)
        settings = _simple_namespace(
            memory_file_for=lambda stage, bid: Path(f"/fake/{bid}_{stage}.md")
        )

        updated_ref_files, _ = _Stage._persist_pass_artifacts(
            ws=ws,
            ticket=FakeTicket(),
            ic=_ic(),
            summary="did work",
            ref_files=["a.py", "b.py"],
            updated_memory="",
            settings=settings,
            memory_board_id="mb",
        )
        assert updated_ref_files == [{"path": "a.py"}, {"path": "b.py"}]
        ref_path = artifacts_dir / "reference_files.json"
        assert ref_path.exists()
        import json

        data = json.loads(ref_path.read_text())
        assert data == [{"path": "a.py"}, {"path": "b.py"}]

    def test_summary_written(self, monkeypatch, tmp_path):
        """implement_summary.md is written and returned as updated_prev_summary."""
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.persist_memory",
            lambda path, text: None,
        )

        artifacts_dir = tmp_path / "artifacts"
        artifacts_dir.mkdir()

        ws = _simple_namespace(artifacts_dir=artifacts_dir)
        settings = _simple_namespace(
            memory_file_for=lambda stage, bid: Path(f"/fake/{bid}_{stage}.md")
        )

        _, updated_prev_summary = _Stage._persist_pass_artifacts(
            ws=ws,
            ticket=FakeTicket(),
            ic=_ic(),
            summary="did the work",
            ref_files=None,
            updated_memory="",
            settings=settings,
            memory_board_id="mb",
        )
        summary_path = artifacts_dir / "implement_summary.md"
        assert summary_path.exists()
        assert summary_path.read_text() == "did the work"
        assert updated_prev_summary == "did the work"


# ---------------------------------------------------------------------------
# 6. summary-claim verification
# ---------------------------------------------------------------------------


class TestVerifySummaryClaims:
    @staticmethod
    def _repo(tmp_path, files=()):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        for f in files:
            p = repo_dir / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        return repo_dir

    def test_changelog_fragment_exists(self, tmp_path):
        repo_dir = self._repo(tmp_path, files=["changelog.d/ticket-1.misc.md"])
        missing = _verify_summary_claims(
            "Changelog fragment created",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_explicit_changelog_d_path_missing(self, tmp_path):
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "wrote changelog.d/20260809T161458Z.misc.md",
            repo_dir,
            "ticket-1",
        )
        assert missing == ["changelog.d/20260809T161458Z.misc.md"]

    def test_generic_created_path_missing(self, tmp_path):
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "created `src/new_module.py`",
            repo_dir,
            "ticket-1",
        )
        assert missing == ["src/new_module.py"]

    def test_bare_basename_resolves_against_branch_changes(self, tmp_path, monkeypatch):
        """'Created 18 tests in test_utils.py' names a file by basename; the
        file lives at tests/robotsix_invest/test_utils.py and is untracked.
        Must NOT be flagged (robotsix-invest dea0 blocked on exactly this)."""
        repo_dir = self._repo(tmp_path, files=["tests/robotsix_invest/test_utils.py"])
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic._collect_changed_files",
            lambda repo, target: {
                "src/robotsix_invest/_utils.py",
                "tests/robotsix_invest/test_utils.py",
            },
        )
        missing = _verify_summary_claims(
            "Created 18 tests in test_utils.py. Registered both new files in "
            "docs/modules.yaml.",
            repo_dir,
            "ticket-1",
        )
        assert "test_utils.py" not in missing

    def test_bare_basename_not_in_branch_changes_is_still_missing(
        self, tmp_path, monkeypatch
    ):
        """A basename that matches nothing the branch touched is a real
        hallucination and keeps being flagged."""
        repo_dir = self._repo(tmp_path)
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic._collect_changed_files",
            lambda repo, target: {"src/other.py"},
        )
        missing = _verify_summary_claims(
            "created helpers.py",
            repo_dir,
            "ticket-1",
        )
        assert missing == ["helpers.py"]

    def test_bare_basename_with_no_git_diff_available_is_still_missing(
        self, tmp_path, monkeypatch
    ):
        repo_dir = self._repo(tmp_path)
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic._collect_changed_files",
            lambda repo, target: None,
        )
        assert _verify_summary_claims("created helpers.py", repo_dir, "t") == [
            "helpers.py"
        ]

    def test_generic_created_path_exists(self, tmp_path):
        repo_dir = self._repo(tmp_path, files=["src/new_module.py"])
        missing = _verify_summary_claims(
            "created `src/new_module.py`",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_non_path_claims_ignored(self, tmp_path):
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "added a test and created a helper function",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_no_changelog_claim(self, tmp_path):
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "Skip-Changelog: no user-facing change",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    # -- prepositional pattern (_CLAIM_X_TO_Y_RE) tests ------------------

    def test_added_x_to_y_prepositional_phrase(self, tmp_path):
        """'added X to Y' where X is a non-path noun → captures Y as path."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "Added a `repo: local` hook `check-trivyignore-expiry` to "
            "`.pre-commit-config.yaml`",
            repo_dir,
            "ticket-1",
        )
        assert missing == [".pre-commit-config.yaml"]

    def test_registered_x_in_y_prepositional(self, tmp_path):
        """'registered X in Y' captures Y as the path."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "registered a new periodic agent `audit.yaml` in "
            "`agent_definitions/periodic/`",
            repo_dir,
            "ticket-1",
        )
        # The direct pattern captures ``audit.yaml`` (after "registered"),
        # and the prepositional pattern captures the container path.
        # Both are claimed new files that don't exist → both flagged.
        assert missing == ["audit.yaml", "agent_definitions/periodic/"]

    def test_added_x_into_y_prepositional(self, tmp_path):
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "added a new test file into `tests/test_foo.py`",
            repo_dir,
            "ticket-1",
        )
        # "tests/test_foo.py" — the path after "into"
        assert missing == ["tests/test_foo.py"]

    def test_added_to_phrase_with_existing_file_and_diff(self, tmp_path):
        """Added-to phrasing with a file that exists but has no git diff
        because there is no git history → gracefully ignored (no
        origin/target_branch to diff against)."""
        repo_dir = self._repo(tmp_path, files=[".pre-commit-config.yaml"])
        missing = _verify_summary_claims(
            "Added a hook to `.pre-commit-config.yaml`",
            repo_dir,
            "ticket-1",
        )
        # File exists, but _collect_changed_files will fail in non-git
        # tmp_path (return set()), so it does NOT flag the file.  This is
        # acceptable — the true claim will be caught by the filesystem
        # check when the file doesn't exist.
        assert missing == []

    def test_added_to_phrase_with_non_existing_file(self, tmp_path):
        """Added-to phrasing pointing at a path that doesn't exist → flagged."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "Added a pre-commit hook to `.pre-commit-config.yaml`",
            repo_dir,
            "ticket-1",
        )
        assert missing == [".pre-commit-config.yaml"]

    # -- _looks_like_path widening tests --------------------------------

    def test_dockerfile_without_extension_looks_like_path(self, tmp_path):
        """Dockerfile (no extension) is now recognised as a valid path."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "created a new Dockerfile for the build stage",
            repo_dir,
            "ticket-1",
        )
        # "Dockerfile" should be captured and flagged as missing.
        assert missing == ["Dockerfile"]

    def test_dockerfile_exists(self, tmp_path):
        repo_dir = self._repo(tmp_path, files=["Dockerfile"])
        missing = _verify_summary_claims(
            "created Dockerfile",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_makefile_without_extension_looks_like_path(self, tmp_path):
        """Makefile (no extension) is now recognised as a valid path."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "wrote a Makefile for the project",
            repo_dir,
            "ticket-1",
        )
        assert missing == ["Makefile"]

    def test_existing_file_in_diff_not_flagged(self, tmp_path):
        """Claiming to have modified an existing file that actually has a
        working-tree diff → passes verification."""
        import subprocess

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("initial")
        (repo_dir / ".gitignore").write_text("__pycache__/")
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test"],
            ["git", "config", "user.name", "Test"],
            ["git", "checkout", "-b", "main"],
            ["git", "add", "."],
            ["git", "commit", "-m", "initial"],
        ]:
            subprocess.run(cmd, cwd=repo_dir, capture_output=True, check=True)

        # Modify README.md (the claimed change actually happened).
        (repo_dir / "README.md").write_text("initial\n\n— updated")
        # Uncommitted working-tree change.

        missing = _verify_summary_claims(
            "Updated README.md with a new section",
            repo_dir,
            "ticket-1",
            target_branch="main",
        )
        assert missing == []

    def test_existing_file_not_in_diff_flagged(self, tmp_path):
        """Claiming to have modified an existing file that is in a branch
        but has no actual diff → flagged as missing."""
        import subprocess

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "README.md").write_text("initial")
        (repo_dir / "setup.py").write_text("setup()")
        (repo_dir / ".gitignore").write_text("__pycache__/")
        for cmd in [
            ["git", "init"],
            ["git", "config", "user.email", "test@test"],
            ["git", "config", "user.name", "Test"],
            ["git", "checkout", "-b", "main"],
            ["git", "add", "."],
            ["git", "commit", "-m", "initial"],
        ]:
            subprocess.run(cmd, cwd=repo_dir, capture_output=True, check=True)
        # Simulate origin/main for diff-base ref.
        subprocess.run(
            ["git", "branch", "origin/main", "main"],
            cwd=repo_dir,
            capture_output=True,
            check=True,
        )

        # Claim we modified README.md — exists but has no working-tree or
        # branch diff (the only diff is setup.py, via working tree).
        (repo_dir / "setup.py").write_text("setup(name='foo')\n")
        missing = _verify_summary_claims(
            "added a section to README.md",
            repo_dir,
            "ticket-1",
            target_branch="main",
        )
        # README.md exists on disk but git diff shows only setup.py
        # changed → should be flagged.
        assert missing == ["README.md"]

    # -- false-positive guards (real blocked-ticket regressions) --------

    def test_backticked_command_is_not_a_path(self, tmp_path):
        """An inline command span ends in `.json` but is not a file claim."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "the SBOM must be generated natively from uv.lock via "
            "`uv export --frozen --format cyclonedx1.5 -o sbom.cdx.json`",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_backticked_http_route_is_not_a_path(self, tmp_path):
        """`GET /wallet/value` is a route, not a file that must exist."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "Added `GET /wallet/value` and `GET /wallet/history` endpoints",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_hyphenated_compound_is_not_a_trigger_verb(self, tmp_path):
        """'glibc-generated lockfile' describes an existing artifact; the
        backticked path two clauses later is not its object."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "npm re-resolves esbuild instead of using the repo's "
            "glibc-generated lockfile; the build emits `dist/vanilla.js`",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_claim_does_not_bind_across_a_sentence_boundary(self, tmp_path):
        """A verb must not reach a path in the following sentence."""
        repo_dir = self._repo(tmp_path)
        missing = _verify_summary_claims(
            "created `src/a.py`. No discrepancy: `sbom.cdx.json` is the "
            "documented CI output filename, not a committed file",
            repo_dir,
            "ticket-1",
        )
        assert missing == ["src/a.py"]

    def test_gitignored_claimed_path_is_skipped(self, tmp_path, monkeypatch):
        """A gitignored path can never appear in a diff, so claiming it
        must not be treated as a hallucination."""
        from robotsix_mill.stages.implement import implementation_logic as il

        repo_dir = self._repo(tmp_path, files=["static/vendor/vanilla.js"])
        monkeypatch.setattr(
            il.git_ops, "ignored_paths", lambda repo, paths: list(paths)
        )
        missing = _verify_summary_claims(
            "wrote `static/vendor/vanilla.js`",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_existing_directory_claim_skips_diff_check(self, tmp_path):
        """A directory is never named in --name-only output, so existing on
        disk is all the diff cross-check can establish."""
        repo_dir = self._repo(tmp_path, files=["agent_definitions/periodic/x.yaml"])
        missing = _verify_summary_claims(
            "registered the agent in `agent_definitions/periodic/`",
            repo_dir,
            "ticket-1",
        )
        assert missing == []

    def test_deleted_changelog_fragment_not_flagged(self, tmp_path):
        """A fragment the summary reports deleting is not claimed to exist."""
        repo_dir = self._repo(tmp_path, files=["changelog.d/ticket-1.removal.md"])
        missing = _verify_summary_claims(
            "Wrote `changelog.d/ticket-1.removal.md`, the correctly named "
            "fragment.\nDeleted the previous pass's mis-named fragment "
            "`changelog.d/20260811T142428Z-old-name-bc04.removal.md`.",
            repo_dir,
            "ticket-1",
        )
        assert missing == []


class TestRunSummaryVerification:
    def test_passes_when_no_claims(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        result = _Stage._run_summary_verification(
            ticket=FakeTicket(),
            repo_dir=repo_dir,
            summary="did the work",
            ic=_ic(),
            updated_ref_files=None,
            updated_prev_summary="did the work",
            new_msgs=None,
        )
        assert result is None

    def test_retry_on_first_failure(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        result = _Stage._run_summary_verification(
            ticket=FakeTicket(),
            repo_dir=repo_dir,
            summary="Created src/pkg/new_module.py with the helper",
            ic=_ic(),
            updated_ref_files=None,
            updated_prev_summary="Created src/pkg/new_module.py with the helper",
            new_msgs=b"ms",
        )
        assert result is not None
        assert result.next_action == "retry"
        assert result.feedback.startswith("[VERIFY] Verification failed:")
        assert result.ic.feedback == result.feedback
        assert (
            result.ic.previous_attempt_summary
            == "Created src/pkg/new_module.py with the helper"
        )
        assert result.new_msgs == b"ms"

    def test_block_on_second_failure(self, tmp_path):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        result = _Stage._run_summary_verification(
            ticket=FakeTicket(),
            repo_dir=repo_dir,
            summary="Created src/pkg/new_module.py with the helper",
            ic=_ic(feedback="[VERIFY] Verification failed: x was claimed ..."),
            updated_ref_files=None,
            updated_prev_summary=None,
            new_msgs=None,
        )
        assert result is not None
        assert result.next_action == "return"
        assert result.outcome.next_state == State.BLOCKED

    def test_accepts_second_failure_when_claimed_path_exists(
        self, tmp_path, monkeypatch
    ):
        """A named-but-undiffed existing file is not a hallucinated claim.

        ``Created ... the actions that .github/workflows/bump.yml
        references`` names a file the pass deliberately left alone.  The
        first pass re-prompts; the second must accept rather than strand
        the ticket, because the file is right there on disk.
        """
        from robotsix_mill.stages.implement import implementation_logic as mod

        repo_dir = tmp_path / "repo"
        (repo_dir / ".github" / "workflows").mkdir(parents=True)
        (repo_dir / ".github" / "workflows" / "bump.yml").write_text("on: push\n")
        monkeypatch.setattr(mod, "_collect_changed_files", lambda *a, **k: {"other.py"})
        summary = (
            "Created the two missing composite actions that "
            ".github/workflows/bump.yml references."
        )
        assert mod._verify_summary_claims(summary, repo_dir, "ticket-1") == [
            ".github/workflows/bump.yml"
        ]
        result = _Stage._run_summary_verification(
            ticket=FakeTicket(),
            repo_dir=repo_dir,
            summary=summary,
            ic=_ic(feedback="[VERIFY] Verification failed: x was claimed ..."),
            updated_ref_files=None,
            updated_prev_summary=None,
            new_msgs=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# 7. _find_insertion_point
# ---------------------------------------------------------------------------


class TestFindInsertionPoint:
    """Tests for ``_find_insertion_point`` — insertion-point hint parsing."""

    @staticmethod
    def _call(spec: str, code: str, file_lines: list[str]) -> int | None:
        return _Stage._find_insertion_point(spec, code, file_lines)

    # -- after imports ---------------------------------------------------

    def test_after_imports(self):
        """'after imports' → after last import line."""
        spec = """After the imports, add:

```python
NEW_CONSTANT = 42
```
"""
        code = "NEW_CONSTANT = 42\n"
        file_lines = [
            "import os\n",
            "import sys\n",
            "\n",
            "x = 1\n",
        ]
        result = self._call(spec, code, file_lines)
        assert result == 2  # after ``import sys``

    def test_after_imports_no_imports_found(self):
        """'after imports' but no imports in file → insert at top."""
        spec = """After the imports, insert:

```python
#!/usr/bin/env python3
```
"""
        code = "#!/usr/bin/env python3\n"
        file_lines = ["x = 1\n", "y = 2\n"]
        result = self._call(spec, code, file_lines)
        assert result == 0

    # -- after line N ----------------------------------------------------

    def test_after_line_n(self):
        spec = """After line 2, add a blank line.

```python

```
"""
        code = "\n"
        file_lines = ["a\n", "b\n", "c\n"]
        result = self._call(spec, code, file_lines)
        assert result == 2

    def test_after_line_n_clamped(self):
        """'after line 100' clamps to file length."""
        spec = """After line 100:

```python
# trailing
```
"""
        code = "# trailing\n"
        file_lines = ["a\n", "b\n"]
        result = self._call(spec, code, file_lines)
        assert result == 2

    # -- before line N ---------------------------------------------------

    def test_before_line_n(self):
        spec = """Before line 3:

```python
# header
```
"""
        code = "# header\n"
        file_lines = ["a\n", "b\n", "c\n", "d\n"]
        result = self._call(spec, code, file_lines)
        assert result == 2  # 0-based index before line 3

    def test_before_line_n_clamped(self):
        """'before line 1' clamps to 0."""
        spec = """Before line 1:

```python
# top
```
"""
        code = "# top\n"
        file_lines = ["a\n", "b\n"]
        result = self._call(spec, code, file_lines)
        assert result == 0

    # -- end of file -----------------------------------------------------

    def test_at_the_end(self):
        spec = """At the end of the file:

```python
# footer
```
"""
        code = "# footer\n"
        file_lines = ["a\n", "b\n"]
        result = self._call(spec, code, file_lines)
        assert result == 2

    def test_end_of_file(self):
        spec = """Append this at the end of file:

```python
# END
```
"""
        code = "# END\n"
        file_lines = ["a\n"]
        result = self._call(spec, code, file_lines)
        assert result == 1

    def test_append(self):
        spec = """Append:

```python
# appended
```
"""
        code = "# appended\n"
        file_lines = ["a\n"]
        result = self._call(spec, code, file_lines)
        assert result == 1

    # -- before class / def ----------------------------------------------

    def test_before_class(self):
        spec = """Before the class:

```python
@dataclass
```
"""
        code = "@dataclass\n"
        file_lines = [
            "import os\n",
            "\n",
            "class Foo:\n",
            "    pass\n",
        ]
        result = self._call(spec, code, file_lines)
        assert result == 2  # index of ``class Foo:``

    def test_before_class_not_found(self):
        """'before class' with no class in file → None."""
        spec = """Before the class:

```python
x = 1
```
"""
        code = "x = 1\n"
        file_lines = ["import os\n", "\n", "y = 2\n"]
        result = self._call(spec, code, file_lines)
        assert result is None

    def test_before_def(self):
        spec = """Before the function:

```python
@lru_cache
```
"""
        code = "@lru_cache\n"
        file_lines = [
            "import os\n",
            "\n",
            "def foo():\n",
            "    pass\n",
        ]
        result = self._call(spec, code, file_lines)
        assert result == 2  # index of ``def foo():``

    def test_before_def_not_found(self):
        """'before def' with no def in file → None."""
        spec = """Before the function:

```python
x = 1
```
"""
        code = "x = 1\n"
        file_lines = ["import os\n", "\n", "y = 2\n"]
        result = self._call(spec, code, file_lines)
        assert result is None

    # -- no hint ---------------------------------------------------------

    def test_no_hint_returns_none(self):
        """No insertion hint in preceding context → None."""
        spec = """Just some description.

```python
x = 1
```
"""
        code = "x = 1\n"
        file_lines = ["a\n", "b\n"]
        result = self._call(spec, code, file_lines)
        assert result is None

    def test_code_not_found_in_spec(self):
        """When the code block isn't found in the spec → None."""
        spec = "No code block here at all."
        code = "x = 1\n"
        file_lines = ["a\n"]
        result = self._call(spec, code, file_lines)
        assert result is None


# ---------------------------------------------------------------------------
# 8. _select_agent_level — retry-loop sentinel
# ---------------------------------------------------------------------------


class TestSelectAgentLevelRetrySentinel:
    """Tests for the sentinel guard that prevents infinite retry loops."""

    @staticmethod
    def _call(ic, repo_dir=None):
        if repo_dir is None:
            repo_dir = Path("/fake/repo")
        return _Stage._select_agent_level(ic, _simple_namespace(), repo_dir, "main")

    def test_sentinel_returns_none(self, tmp_path):
        """When previous attempt was a failed spec-exact bypass, return None."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "m.py").write_text("# real")

        spec = """### `src/m.py`

```python
# code
```
"""
        ic = _ic(
            spec=spec,
            previous_attempt_summary="spec-exact bypass: failed — 3 block(s) unapplied",
        )
        result = self._call(ic, repo_dir=repo_dir)
        assert result is None  # Falls through to LLM, not -1

    def test_successful_spec_exact_still_returns_neg1(self, tmp_path):
        """A successful spec-exact edit summary does NOT trigger the sentinel."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "m.py").write_text("# real")

        spec = """### `src/m.py`

```python
# code
```
"""
        ic = _ic(
            spec=spec,
            previous_attempt_summary="spec-exact edit: 3 file(s) changed — src/a.py, src/b.py, src/c.py",
        )
        result = self._call(ic, repo_dir=repo_dir)
        assert result == -1  # Still enters spec-exact path


# ---------------------------------------------------------------------------
# 9. _handle_spec_exact_edits — strategy application and failure paths
# ---------------------------------------------------------------------------


class TestHandleSpecExactEdits:
    """Tests for ``_handle_spec_exact_edits`` covering all three strategies
    plus the no-edit and guardrail-continue paths."""

    @staticmethod
    def _make_dummy_ctx():
        """Return a minimal StageContext with a mock service."""
        svc = SimpleNamespace()
        ws = SimpleNamespace()
        ws.artifacts_dir = Path("/tmp/artifacts")
        svc.workspace = lambda ticket: ws
        svc.add_step_event = lambda tid, msg: None
        svc.set_implement_cycles = lambda tid, n: None
        return SimpleNamespace(service=svc, repo_config=None)

    @staticmethod
    def _patch_persist(monkeypatch):
        """Prevent ``_persist_pass_artifacts`` from touching the real fs."""
        monkeypatch.setattr(
            _Stage,
            "_persist_pass_artifacts",
            lambda ws, ticket, ic, summary, ref_files, updated_memory, settings, memory_board_id: (
                ref_files,
                summary,
            ),
        )

    @staticmethod
    def _patch_guardrail_proceed(monkeypatch):
        """Make ``_run_scope_guardrail`` return action='skip_iteration'."""
        from robotsix_mill.stages.implement._shared import _ScopeGuardrailResult

        monkeypatch.setattr(
            _Stage,
            "_run_scope_guardrail",
            lambda *a, **kw: _ScopeGuardrailResult(action="skip_iteration"),
        )

    @staticmethod
    def _patch_evaluate(monkeypatch):
        """Make ``_evaluate_test_results`` return a proceed result."""
        monkeypatch.setattr(
            _Stage,
            "_evaluate_test_results",
            lambda *a, **kw: _SinglePassResult(
                next_action="proceed",
                outcome=Outcome(State.CODE_REVIEW, "ok"),
            ),
        )

    def test_unified_diff_strategy(self, tmp_path, monkeypatch):
        """A spec code block that looks like a unified diff is applied via patch."""
        import subprocess as sp

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        target = repo_dir / "src" / "f.py"
        target.write_text("line1\nline2\nline3\n")

        spec = """### `src/f.py`

```diff
--- src/f.py
+++ src/f.py
@@ -1,3 +1,3 @@
 line1
-line2
+line2-changed
 line3
```
"""
        self._patch_persist(monkeypatch)
        self._patch_guardrail_proceed(monkeypatch)
        self._patch_evaluate(monkeypatch)

        # Mock subprocess.run so ``patch`` (which may not be available
        # in the test sandbox) returns the expected patched content.
        original_run = sp.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[0] == "patch":
                return sp.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout="line1\nline2-changed\nline3\n",
                )
            return original_run(cmd, *args, **kwargs)

        monkeypatch.setattr(sp, "run", fake_run)

        ic = _ic(spec=spec)
        ctx = self._make_dummy_ctx()

        result = _Stage._handle_spec_exact_edits(
            ctx,
            FakeTicket(),
            repo_dir,
            "main",
            _simple_namespace(),
            ic,
            "main",
            None,
        )
        assert result.next_action == "proceed"
        content = target.read_text()
        assert "line2-changed" in content

    def test_context_aware_replacement(self, tmp_path, monkeypatch):
        """A code block matching file content is replaced in-place."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        target = repo_dir / "src" / "f.py"
        target.write_text("import os\n\ndef foo():\n    return 1\n")

        spec = """### `src/f.py`

```python
import os

def foo():
    return 42
```
"""
        self._patch_persist(monkeypatch)
        self._patch_guardrail_proceed(monkeypatch)
        self._patch_evaluate(monkeypatch)

        ic = _ic(spec=spec)
        ctx = self._make_dummy_ctx()

        result = _Stage._handle_spec_exact_edits(
            ctx,
            FakeTicket(),
            repo_dir,
            "main",
            _simple_namespace(),
            ic,
            "main",
            None,
        )
        assert result.next_action == "proceed"
        content = target.read_text()
        assert "return 42" in content

    def test_insertion_point_hints(self, tmp_path, monkeypatch):
        """Insertion via 'after imports' hint."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        target = repo_dir / "src" / "f.py"
        target.write_text("import os\nimport sys\n\nx = 1\n")

        spec = """### `src/f.py`

After the imports, insert:

```python
from pathlib import Path
```
"""
        self._patch_persist(monkeypatch)
        self._patch_guardrail_proceed(monkeypatch)
        self._patch_evaluate(monkeypatch)

        ic = _ic(spec=spec)
        ctx = self._make_dummy_ctx()

        result = _Stage._handle_spec_exact_edits(
            ctx,
            FakeTicket(),
            repo_dir,
            "main",
            _simple_namespace(),
            ic,
            "main",
            None,
        )
        assert result.next_action == "proceed"
        content = target.read_text()
        assert "from pathlib import Path" in content

    def test_no_edits_fallthrough(self, tmp_path, monkeypatch):
        """When no strategy applies, the retry result includes a sentinel ic."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        target = repo_dir / "src" / "f.py"
        target.write_text("completely different content\n")

        spec = """### `src/f.py`

No hints, no diff, no matching context.

```python
x = 999
```
"""
        ic = _ic(spec=spec)
        ctx = self._make_dummy_ctx()

        result = _Stage._handle_spec_exact_edits(
            ctx,
            FakeTicket(),
            repo_dir,
            "main",
            _simple_namespace(),
            ic,
            "main",
            None,
        )
        assert result.next_action == "retry"
        assert result.ic is not None
        assert result.ic.previous_attempt_summary is not None
        assert result.ic.previous_attempt_summary.startswith(
            "spec-exact bypass: failed"
        )

    def test_empty_blocks(self, tmp_path, monkeypatch):
        """Zero code blocks → no edits applied, returns sentinel retry."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()

        spec = "No code blocks at all."
        ic = _ic(spec=spec)
        ctx = self._make_dummy_ctx()

        result = _Stage._handle_spec_exact_edits(
            ctx,
            FakeTicket(),
            repo_dir,
            "main",
            _simple_namespace(),
            ic,
            "main",
            None,
        )
        assert result.next_action == "retry"
        assert result.ic is not None
        assert result.ic.previous_attempt_summary.startswith(
            "spec-exact bypass: failed"
        )

    def test_partial_success(self, tmp_path, monkeypatch):
        """One file succeeds, another fails → proceeds with summary noting skip."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "src" / "a.py").write_text("import os\n\nx = 1\n")
        # src/b.py does NOT exist.

        spec = """### `src/a.py`

```python
import os

x = 42
```

### `src/b.py`

```python
# missing file
```
"""
        self._patch_persist(monkeypatch)
        self._patch_guardrail_proceed(monkeypatch)
        self._patch_evaluate(monkeypatch)

        ic = _ic(spec=spec)
        ctx = self._make_dummy_ctx()

        result = _Stage._handle_spec_exact_edits(
            ctx,
            FakeTicket(),
            repo_dir,
            "main",
            _simple_namespace(),
            ic,
            "main",
            None,
        )
        assert result.next_action == "proceed"

    def test_guardrail_continue(self, tmp_path, monkeypatch):
        """When the guardrail returns 'continue', we retry with updated ic."""
        from robotsix_mill.stages.implement._shared import _ScopeGuardrailResult

        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "src").mkdir(parents=True)
        target = repo_dir / "src" / "f.py"
        target.write_text("import os\n\nx = 1\n")

        spec = """### `src/f.py`

```python
import os

x = 42
```
"""
        self._patch_persist(monkeypatch)
        # Guardrail returns "continue" → retry with feedback.
        monkeypatch.setattr(
            _Stage,
            "_run_scope_guardrail",
            lambda *a, **kw: _ScopeGuardrailResult(
                action="continue",
                feedback="scope guardrail: some files out of scope",
            ),
        )

        ic = _ic(spec=spec)
        ctx = self._make_dummy_ctx()

        result = _Stage._handle_spec_exact_edits(
            ctx,
            FakeTicket(),
            repo_dir,
            "main",
            _simple_namespace(),
            ic,
            "main",
            None,
        )
        assert result.next_action == "retry"
        assert result.ic is not None
        assert result.ic.feedback == "scope guardrail: some files out of scope"
