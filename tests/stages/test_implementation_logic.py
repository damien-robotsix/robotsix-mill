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
from robotsix_mill.stages.implement._shared import (
    _ImplementContext,
)
from robotsix_mill.stages.implement.core import ImplementStage

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ic(**overrides) -> _ImplementContext:
    defaults: dict = dict(
        spec="Add feature",
        memory_text="",
        reference_files=None,
        file_map=None,
        feedback=None,
        previous_attempt_summary=None,
        open_thread_ids=None,
    )
    defaults.update(overrides)
    return _ImplementContext(**defaults)


def _simple_namespace(**kw):
    return SimpleNamespace(**kw)


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
    def test_no_change_in_summary(self):
        ic = _ic(previous_attempt_summary="no change needed after inspection")
        settings = _simple_namespace()
        result = _Stage._select_agent_level(ic, settings)
        assert result == 1

    def test_no_change_in_feedback(self):
        ic = _ic(feedback="NO CHANGE NEEDED — already satisfied")
        settings = _simple_namespace()
        result = _Stage._select_agent_level(ic, settings)
        assert result == 1

    def test_no_change_in_both_fields(self):
        ic = _ic(
            previous_attempt_summary="summary text",
            feedback="feedback with No Change Needed here",
        )
        settings = _simple_namespace()
        result = _Stage._select_agent_level(ic, settings)
        assert result == 1

    def test_no_phrase_returns_none(self):
        ic = _ic(
            previous_attempt_summary="everything looks fine",
            feedback="tests pass",
        )
        settings = _simple_namespace()
        result = _Stage._select_agent_level(ic, settings)
        assert result is None

    def test_none_fields_returns_none(self):
        ic = _ic(previous_attempt_summary=None, feedback=None)
        settings = _simple_namespace()
        result = _Stage._select_agent_level(ic, settings)
        assert result is None

    def test_empty_strings_returns_none(self):
        ic = _ic(previous_attempt_summary="", feedback="")
        settings = _simple_namespace()
        result = _Stage._select_agent_level(ic, settings)
        assert result is None


# ---------------------------------------------------------------------------
# 2. _invoke_implement_agent
# ---------------------------------------------------------------------------


class FakeTicket:
    id = "test-ticket-1"
    implement_cycles = 0


def _stage_ctx(**kw):
    defaults = dict(repo_config=None)
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
            finalize_calls.append(dict(ok=ok, summary=summary))

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
            ),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.git_ops",
            _simple_namespace(introduced_files=lambda rd, tgt: []),
        )
        monkeypatch.setattr(
            "robotsix_mill.stages.implement.implementation_logic.acknowledge_unanswered_threads",
            lambda *a: None,
        )

    @staticmethod
    def _call(monkeypatch, **overrides):
        """Call _evaluate_test_results with common defaults + overrides.

        IMPORTANT: callers must invoke ``_install_default_patches(monkeypatch)``
        BEFORE any test-specific monkeypatch overrides, THEN call this method.
        """
        params: dict = dict(
            ctx=_simple_namespace(
                repo_config=None,
                service=_simple_namespace(
                    add_step_event=lambda *a: None,
                    set_implement_cycles=lambda *a: None,
                ),
            ),
            ticket=FakeTicket(),
            repo_dir=_DUMMY_PATH,
            branch="main",
            settings=_simple_namespace(
                review_enabled=True,
                smoke_command="",
            ),
            ic=_ic(),
            new_ic=_ic(),
            summary="did work",
            ref_files=None,
            new_msgs=None,
            no_change_needed=False,
            no_change_rationale="",
            resuming=False,
            attempt=1,
            max_iters=3,
            extra_roots=None,
        )
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
            _simple_namespace(introduced_files=_fake_introduced_files),
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
            "robotsix_mill.stages.implement.implementation_logic.get_repo_config",
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
