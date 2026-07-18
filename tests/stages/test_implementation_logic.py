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

    def test_budget_error_saves_conversation_state(self, monkeypatch, tmp_path):
        """AgentBudgetError with conversation_state + ws → saves state."""
        fake_conv_state = b'{"messages": ["test"]}'
        monkeypatch.setattr(
            "robotsix_mill.agents.coding.run_implement_agent",
            lambda **kw: (_ for _ in ()).throw(
                AgentBudgetError(
                    "cap hit", [], conversation_state=fake_conv_state
                )
            ),
        )

        finalize_calls = []

        def _fake_finalize(cls, ctx, ticket, repo_dir, branch, summary, *, ok, **kw):
            finalize_calls.append(dict(ok=ok, summary=summary))

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
                AgentBudgetError(
                    "cap hit", [], conversation_state=fake_conv_state
                )
            ),
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
