"""Unit tests for MultiRepoCiFixMixin (ci_fix_mixin.py).

Covers all three private methods:
- _multi_repo_fix_ci
- _note_ci_fix_attempt
- _try_multi_codeql_fp_triage
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from robotsix_mill.config import ConfigError
from robotsix_mill.core.states import State
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.merge.ci_fix_mixin import MultiRepoCiFixMixin


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def mixin():
    """Plain mixin instance with no patched dependencies."""
    return MultiRepoCiFixMixin()


@pytest.fixture
def ticket():
    t = MagicMock()
    t.id = "T-001"
    return t


@pytest.fixture
def settings():
    s = MagicMock()
    s.ci_fix_max_attempts = 3
    s.ci_fix_max_cycles = 5
    s.codeql_fp_triage_enabled = False
    s.memory_file_for.return_value = Path("/tmp/ci_fix_mem.json")
    s.forge_target_branch = "main"
    return s


@pytest.fixture
def workspace(tmp_path):
    ws = MagicMock()
    ws.dir = tmp_path
    ws.artifacts_dir = tmp_path / "artifacts"
    ws.artifacts_dir.mkdir()
    return ws


@pytest.fixture
def service(workspace):
    svc = MagicMock()
    svc.workspace.return_value = workspace
    svc.add_history_note.return_value = None
    return svc


@pytest.fixture
def ctx(settings, service):
    c = MagicMock()
    c.settings = settings
    c.service = service
    return c


@pytest.fixture
def status():
    return {"repo_id": "test-repo", "branch": "feature/x"}


@pytest.fixture
def repo_dir(tmp_path):
    (tmp_path / "repos" / "test-repo" / ".git").mkdir(parents=True)
    return tmp_path / "repos" / "test-repo"


@pytest.fixture
def repo_config():
    rc = MagicMock()
    rc.repo_id = "board-1"
    return rc


@pytest.fixture
def forge():
    f = MagicMock()
    f.check_status.return_value = {"conclusion": "failure", "failing": []}
    f.list_code_scanning_alerts.return_value = []
    f.pr_status.return_value = {"sha": "abc123"}
    f.list_workflow_runs.return_value = []
    f.dismiss_code_scanning_alert.return_value = True
    return f


# ---------------------------------------------------- _note_ci_fix_attempt


def test_note_ci_fix_attempt_success(mixin, ctx, ticket):
    """Records a breadcrumb via add_history_note."""
    mixin._note_ci_fix_attempt(ctx, ticket.id, "a note")
    ctx.service.add_history_note.assert_called_once_with(ticket.id, "a note")


def test_note_ci_fix_attempt_failure_is_swallowed(mixin, ctx, ticket):
    """add_history_note raising does not propagate."""
    ctx.service.add_history_note.side_effect = RuntimeError("boom")
    # Must not raise.
    mixin._note_ci_fix_attempt(ctx, ticket.id, "a note")


# ---------------------------------------------------- _multi_repo_fix_ci


class TestMultiRepoFixCi:
    @pytest.fixture(autouse=True)
    def _repo_git_dir(self, ctx, ticket, status):
        """Ensure the repo clone dir exists for tests that need it.

        Individual tests can delete .git to test the missing-clone path.
        """
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repos" / status["repo_id"] / ".git"
        repo_dir.mkdir(parents=True, exist_ok=True)

    @pytest.fixture(autouse=True)
    def _patch_facade(self):
        """Mock the lazy ``from robotsix_mill.stages import merge as _facade``.

        The method body does ``from robotsix_mill.stages import merge as
        _facade`` — patching the module that the statement imports is
        enough; the local ``_facade`` name will be the mock.
        """
        with patch(
            "robotsix_mill.stages.merge",
            autospec=False,
        ) as mock_merge:
            yield mock_merge

    @pytest.fixture(autouse=True)
    def _patch_get_repo_config(self, repo_config):
        with patch(
            "robotsix_mill.stages.merge.ci_fix_mixin.get_repo_config",
            return_value=repo_config,
        ) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_get_forge(self, forge):
        with patch(
            "robotsix_mill.stages.merge.ci_fix_mixin.get_forge",
            return_value=forge,
        ) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_read_counter(self):
        with patch(
            "robotsix_mill.stages.merge.ci_fix_mixin._read_counter",
            return_value=0,
        ) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_write_counter(self):
        with patch("robotsix_mill.stages.merge.ci_fix_mixin._write_counter") as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_build_failing_summary(self):
        with patch(
            "robotsix_mill.stages.merge.ci_fix_mixin._build_failing_summary",
            return_value="failing summary",
        ) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_note_attempt(self):
        with patch.object(MultiRepoCiFixMixin, "_note_ci_fix_attempt") as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_try_triage(self):
        with patch.object(
            MultiRepoCiFixMixin,
            "_try_multi_codeql_fp_triage",
            return_value=None,
        ) as mock:
            yield mock

    # -- early-exit paths ---------------------------------------------------

    def test_missing_clone_blocks(self, mixin, ticket, ctx, status):
        """No .git dir → BLOCKED immediately."""
        ws = ctx.service.workspace(ticket)
        repo_dir = ws.dir / "repos" / status["repo_id"]
        # Remove the .git dir that the autouse fixture created.
        (repo_dir / ".git").rmdir()
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == State.BLOCKED
        assert "missing" in outcome.note

    def test_unknown_repo_id_blocks(
        self, mixin, ticket, ctx, status, _patch_get_repo_config
    ):
        """get_repo_config raises ConfigError → BLOCKED."""
        _patch_get_repo_config.side_effect = ConfigError("no such repo")
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == State.BLOCKED
        assert "unknown repo_id" in outcome.note

    def test_check_status_transient_error_repolls(
        self, mixin, ticket, ctx, status, forge
    ):
        """check_status raises → re-poll with current state, no attempt counted."""
        forge.check_status.side_effect = ConnectionError("transient")
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == ticket.state

    # -- CI already green ---------------------------------------------------

    def test_ci_already_green_resets_cycles(self, mixin, ticket, ctx, status, forge):
        """conclusion=success → reset cycle counter and re-poll."""
        forge.check_status.return_value = {"conclusion": "success"}
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == ticket.state

    # -- FP triage interception ---------------------------------------------

    def test_fp_triage_intercepts_before_attempt_cap(
        self, mixin, ticket, ctx, status, forge, _patch_try_triage
    ):
        """When triage returns an Outcome, it short-circuits."""
        intercept = Outcome(State.BLOCKED, "triage blocked it")
        _patch_try_triage.return_value = intercept
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome is intercept

    # -- attempt cap exhausted ----------------------------------------------

    def test_attempt_cap_exhausted_blocks(
        self,
        mixin,
        ticket,
        ctx,
        status,
        forge,
        _patch_read_counter,
    ):
        """counter >= max_attempts → BLOCKED."""
        _patch_read_counter.return_value = 3  # attempt = 4 > max=3
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == State.BLOCKED
        assert "manual intervention" in outcome.note

    def test_attempt_cap_with_codeql_block_note(
        self, mixin, ticket, ctx, status, forge, _patch_read_counter
    ):
        """When _codeql_block_note returns a string, it appears in the note."""
        _patch_read_counter.return_value = 3
        forge.check_status.return_value = {
            "conclusion": "failure",
            "failing": [{"name": "CodeQL"}],
        }
        forge.list_code_scanning_alerts.return_value = [
            {"number": 1, "most_recent_instance": {"location": {"path": "a.py"}}}
        ]
        forge.pr_status.return_value = {"sha": "abc123"}
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == State.BLOCKED

    # -- cycle ceiling ------------------------------------------------------

    def test_cycle_ceiling_exhausted_blocks(
        self,
        mixin,
        ticket,
        ctx,
        status,
        forge,
        settings,
    ):
        """ci_fix_max_cycles=1, cycles counter=1 → BLOCKED."""
        settings.ci_fix_max_cycles = 1
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}

        with patch("robotsix_mill.stages.merge.ci_fix_mixin._read_counter") as rc_mock:
            # First read: attempt counter (before cap check).  Second read:
            # cycle counter.  Third read: attempt counter again (inside
            # main flow, but we're aborting at ceiling).
            rc_mock.side_effect = [0, 1]
            outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == State.BLOCKED
        assert "hard ceiling" in outcome.note

    # -- reconcile DIVERGED -------------------------------------------------

    def test_reconcile_diverged_blocks(
        self, mixin, ticket, ctx, status, forge, _patch_facade
    ):
        """ReconcileResult.DIVERGED → BLOCKED."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.git_ops.ReconcileResult.DIVERGED = "DIVERGED"
        _patch_facade.git_ops.reconcile_with_remote_pr.return_value = "DIVERGED"
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == State.BLOCKED
        assert "diverged" in outcome.note.lower()

    # -- reconcile UNAVAILABLE (proceeds, push_with_lease backstops) ---------

    def test_reconcile_unavailable_proceeds(
        self, mixin, ticket, ctx, status, forge, _patch_facade
    ):
        """UNAVAILABLE logs warning but continues to agent run."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.git_ops.ReconcileResult.UNAVAILABLE = "UNAVAILABLE"
        _patch_facade.git_ops.reconcile_with_remote_pr.return_value = "UNAVAILABLE"
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.git_ops.post_push_check.return_value = (
            _patch_facade.git_ops.PostPushResult.PASS
        )
        _patch_facade.run_ci_fix_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == ticket.state  # re-poll on success

    # -- agent success ------------------------------------------------------

    def test_agent_done_push_verified_repolls(
        self, mixin, ticket, ctx, status, forge, _patch_facade
    ):
        """Agent DONE + post_push_check PASS → reset counter, re-poll."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.git_ops.post_push_check.return_value = (
            _patch_facade.git_ops.PostPushResult.PASS
        )
        _patch_facade.run_ci_fix_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == ticket.state

    def test_agent_done_no_changes_still_counts(
        self, mixin, ticket, ctx, status, forge, _patch_facade
    ):
        """DONE but head==remote → counts toward cap, re-poll."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha1"
        _patch_facade.run_ci_fix_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == ticket.state

    def test_post_push_check_fail_blocks(
        self, mixin, ticket, ctx, status, forge, _patch_facade
    ):
        """PostPushResult != PASS → BLOCKED."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.git_ops.post_push_check.return_value = "STALE"
        _patch_facade.run_ci_fix_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == State.BLOCKED
        assert "post-check failed" in outcome.note

    def test_post_push_check_error_blocks(
        self, mixin, ticket, ctx, status, forge, _patch_facade
    ):
        """post_push_check raises → BLOCKED."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.git_ops.post_push_check.side_effect = ValueError("boom")
        _patch_facade.run_ci_fix_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == State.BLOCKED
        assert "post-check error" in outcome.note

    # -- agent failure ------------------------------------------------------

    def test_agent_crashed_repolls(
        self, mixin, ticket, ctx, status, forge, _patch_facade
    ):
        """run_ci_fix_agent raises → re-poll (attempt counted)."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.run_ci_fix_agent.side_effect = RuntimeError("crash")
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == ticket.state

    def test_agent_not_done_repolls(
        self, mixin, ticket, ctx, status, forge, _patch_facade
    ):
        """Agent status != 'DONE' → re-poll."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.run_ci_fix_agent.return_value = MagicMock(
            status="FAILED", updated_memory=None
        )
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        assert outcome.next_state == ticket.state

    # -- ci=None edge case --------------------------------------------------

    def test_ci_none_not_green(self, mixin, ticket, ctx, status, forge):
        """check_status returns None → not treated as success."""
        forge.check_status.return_value = None
        outcome = mixin._multi_repo_fix_ci(ticket, ctx, status)
        # None.conclusion is None → not "success" → proceeds to attempt cap
        assert outcome.next_state == ticket.state  # re-poll, not BLOCKED yet

    # -- persisted memory ---------------------------------------------------

    def test_agent_persists_updated_memory(
        self, mixin, ticket, ctx, status, forge, _patch_facade, settings
    ):
        """updated_memory is passed to persist_memory."""
        forge.check_status.return_value = {"conclusion": "failure", "failing": []}
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.git_ops.post_push_check.return_value = (
            _patch_facade.git_ops.PostPushResult.PASS
        )
        _patch_facade.run_ci_fix_agent.return_value = MagicMock(
            status="DONE", updated_memory='{"key": "val"}'
        )
        mixin._multi_repo_fix_ci(ticket, ctx, status)
        _patch_facade.persist_memory.assert_called_once()


# ---------------------------------------------------- _try_multi_codeql_fp_triage


class TestTryMultiCodeqlFpTriage:
    @pytest.fixture(autouse=True)
    def _patch_get_forge(self, forge):
        with patch(
            "robotsix_mill.stages.merge.ci_fix_mixin.get_forge",
            return_value=forge,
        ) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_run_triage_agent(self):
        with patch(
            "robotsix_mill.agents.codeql_fp_triage.run_codeql_fp_triage_agent",
        ) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_only_codeql_failing(self):
        with patch(
            "robotsix_mill.stages.merge.ci_fix_mixin._only_codeql_failing",
            return_value=True,
        ) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_eligible_for_triage(self):
        with patch(
            "robotsix_mill.stages.merge.ci_fix_mixin._eligible_for_triage",
            return_value=[{"number": 42, "path": "a.py"}],
        ) as mock:
            yield mock

    @pytest.fixture(autouse=True)
    def _patch_get_repo_config(self, repo_config):
        # The triage method has a lazy ``from ...config import get_repo_config``
        # inside the method body — patch the source module, not ci_fix_mixin.
        with patch(
            "robotsix_mill.config.get_repo_config",
            return_value=repo_config,
        ) as mock:
            yield mock

    @pytest.fixture
    def alerts(self):
        return [{"number": 42, "most_recent_instance": {"location": {"path": "a.py"}}}]

    @pytest.fixture
    def changed_paths(self):
        return {"a.py"}

    # -- feature flag off ---------------------------------------------------

    def test_feature_flag_off_returns_none(
        self, mixin, ticket, ctx, settings, repo_dir
    ):
        """Disabled → None (caller proceeds normally)."""
        settings.codeql_fp_triage_enabled = False
        result = mixin._try_multi_codeql_fp_triage(
            ticket, ctx, [], [], set(), "repo", repo_dir
        )
        assert result is None

    # -- non-codeql checks still failing ------------------------------------

    def test_non_codeql_failing_returns_none(
        self, mixin, ticket, ctx, settings, repo_dir, _patch_only_codeql_failing
    ):
        """Still have non-CodeQL checks → None."""
        settings.codeql_fp_triage_enabled = True
        _patch_only_codeql_failing.return_value = False
        result = mixin._try_multi_codeql_fp_triage(
            ticket, ctx, [], [], set(), "repo", repo_dir
        )
        assert result is None

    # -- sentinel already exists --------------------------------------------

    def test_sentinel_exists_returns_none(
        self, mixin, ticket, ctx, settings, workspace, repo_dir
    ):
        """Run-once sentinel file exists → None."""
        settings.codeql_fp_triage_enabled = True
        sentinel = workspace.artifacts_dir / "codeql_fp_triage_ran.txt"
        sentinel.write_text("1")
        result = mixin._try_multi_codeql_fp_triage(
            ticket, ctx, [], [], set(), "repo", repo_dir
        )
        assert result is None

    # -- no eligible alerts -------------------------------------------------

    def test_no_eligible_alerts_returns_none(
        self, mixin, ticket, ctx, settings, repo_dir, _patch_eligible_for_triage
    ):
        """_eligible_for_triage returns [] → None."""
        settings.codeql_fp_triage_enabled = True
        _patch_eligible_for_triage.return_value = []
        result = mixin._try_multi_codeql_fp_triage(
            ticket, ctx, [], [], set(), "repo", repo_dir
        )
        assert result is None

    # -- agent crash --------------------------------------------------------

    def test_agent_crash_returns_none(
        self, mixin, ticket, ctx, settings, repo_dir, _patch_run_triage_agent
    ):
        """run_codeql_fp_triage_agent raises → None."""
        settings.codeql_fp_triage_enabled = True
        _patch_run_triage_agent.side_effect = RuntimeError("crash")
        result = mixin._try_multi_codeql_fp_triage(
            ticket, ctx, [], [], set(), "repo", repo_dir
        )
        assert result is None

    # -- agent abstains -----------------------------------------------------

    def test_agent_abstains_returns_none(
        self, mixin, ticket, ctx, settings, repo_dir, _patch_run_triage_agent
    ):
        """All verdicts are 'abstain' → None."""
        settings.codeql_fp_triage_enabled = True
        verdict = MagicMock()
        verdict.verdict = "abstain"
        verdict.alert_number = 42
        verdict.rationale = "seems valid"
        _patch_run_triage_agent.return_value = MagicMock(verdicts=[verdict])
        result = mixin._try_multi_codeql_fp_triage(
            ticket, ctx, [], [], set(), "repo", repo_dir
        )
        assert result is None

    # -- successful dismissal -----------------------------------------------

    def test_dismissal_success_repolls(
        self,
        mixin,
        ticket,
        ctx,
        settings,
        forge,
        repo_dir,
        _patch_run_triage_agent,
    ):
        """At least one alert dismissed → Outcome(ticket.state)."""
        settings.codeql_fp_triage_enabled = True
        verdict = MagicMock()
        verdict.verdict = "dismiss"
        verdict.alert_number = 42
        verdict.rationale = "false positive"
        _patch_run_triage_agent.return_value = MagicMock(verdicts=[verdict])
        result = mixin._try_multi_codeql_fp_triage(
            ticket, ctx, [], [], set(), "repo", repo_dir
        )
        assert result is not None
        assert result.next_state == ticket.state
        forge.dismiss_code_scanning_alert.assert_called_once()

    # -- dismissal API failure keeps going ----------------------------------

    def test_dismissal_api_failure_logged(
        self,
        mixin,
        ticket,
        ctx,
        settings,
        forge,
        repo_dir,
        _patch_run_triage_agent,
    ):
        """dismiss_code_scanning_alert returns False → not counted."""
        settings.codeql_fp_triage_enabled = True
        forge.dismiss_code_scanning_alert.return_value = False
        verdict = MagicMock()
        verdict.verdict = "dismiss"
        verdict.alert_number = 42
        verdict.rationale = "false positive"
        _patch_run_triage_agent.return_value = MagicMock(verdicts=[verdict])
        result = mixin._try_multi_codeql_fp_triage(
            ticket, ctx, [], [], set(), "repo", repo_dir
        )
        # dismissed_count stays 0 → returns None (not Outcome)
        assert result is None

    # -- sends sentinel -----------------------------------------------------

    def test_sets_sentinel_after_check(
        self,
        mixin,
        ticket,
        ctx,
        settings,
        workspace,
        repo_dir,
        _patch_run_triage_agent,
    ):
        """Sentinel file is created so second invocation skips."""
        settings.codeql_fp_triage_enabled = True
        _patch_run_triage_agent.return_value = MagicMock(
            verdicts=[MagicMock(verdict="dismiss", alert_number=42, rationale="fp")]
        )
        mixin._try_multi_codeql_fp_triage(ticket, ctx, [], [], set(), "repo", repo_dir)
        sentinel = workspace.artifacts_dir / "codeql_fp_triage_ran.txt"
        assert sentinel.exists()
