"""Unit tests for ReviewRevisionMixin (review_revision.py).

Covers both private methods:
- _run_review_revision
- _review_changes_requested_outcome
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from robotsix_mill.core.states import State
from robotsix_mill.stages.merge.review_revision import ReviewRevisionMixin


# ------------------------------------------------------------------ fixtures


@pytest.fixture
def mixin():
    """Plain mixin instance with no patched dependencies."""
    return ReviewRevisionMixin()


@pytest.fixture
def ticket():
    t = MagicMock()
    t.id = "T-001"
    t.branch = None
    return t


@pytest.fixture
def settings():
    s = MagicMock()
    s.branch_prefix = "mill/"
    s.review_revision_max_attempts = 3
    s.review_feedback_enabled = True
    s.memory_file_for.return_value = Path("/tmp/review_revision_mem.json")
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
    return svc


@pytest.fixture
def ctx(settings, service):
    c = MagicMock()
    c.settings = settings
    c.service = service
    c.repo_config = MagicMock()
    c.memory_board_id.return_value = "board-1"
    return c


@pytest.fixture
def repo_dir(tmp_path):
    d = tmp_path / "repos" / "test-repo"
    (d / ".git").mkdir(parents=True)
    return d


@pytest.fixture
def feedback():
    return {
        "comments": [
            {"path": "a.py", "line": 10, "body": "fix this"},
            {"path": "b.py", "line": None, "body": "also fix"},
        ],
        "files": ["a.py", "b.py"],
    }


@pytest.fixture
def forge():
    f = MagicMock()
    f.pr_review_status.return_value = {
        "state": "CHANGES_REQUESTED",
        "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
        "body": "",
    }
    return f


# =================================================== _run_review_revision


class TestRunReviewRevision:
    @pytest.fixture(autouse=True)
    def _patch_facade(self):
        """Mock the lazy ``from robotsix_mill.stages import merge as _facade``."""
        with patch(
            "robotsix_mill.stages.merge",
            autospec=False,
        ) as mock_merge:
            yield mock_merge

    @pytest.fixture(autouse=True)
    def _setup_repo_dir(self, ctx, ticket, workspace, repo_dir, _patch_facade):
        """Wire _workspace_repo_dir to return repo_dir by default."""
        _patch_facade._workspace_repo_dir.return_value = str(repo_dir)

    @pytest.fixture(autouse=True)
    def _write_feedback_artifact(self, workspace, feedback):
        """Write a default review_feedback.json so tests can override per-case."""
        artifact_dir = workspace.artifacts_dir
        artifact_dir.joinpath("review_feedback.json").write_text(
            json.dumps(feedback), encoding="utf-8"
        )

    # -- missing clone ---------------------------------------------------

    def test_missing_clone_blocks(self, mixin, ticket, ctx, _patch_facade):
        """No workspace clone → BLOCKED."""
        _patch_facade._workspace_repo_dir.return_value = None
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.BLOCKED
        assert "missing" in outcome.note

    # -- missing / corrupt artifact --------------------------------------

    def test_missing_feedback_artifact_routes_human_mr(
        self, mixin, ticket, ctx, workspace
    ):
        """Missing review_feedback.json → HUMAN_MR_APPROVAL."""
        (workspace.artifacts_dir / "review_feedback.json").unlink()
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.HUMAN_MR_APPROVAL

    def test_corrupt_feedback_artifact_routes_human_mr(
        self, mixin, ticket, ctx, workspace
    ):
        """Corrupt JSON → HUMAN_MR_APPROVAL."""
        (workspace.artifacts_dir / "review_feedback.json").write_text(
            "not json", encoding="utf-8"
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.HUMAN_MR_APPROVAL

    def test_empty_comments_routes_human_mr(self, mixin, ticket, ctx, workspace):
        """Empty comments list → HUMAN_MR_APPROVAL."""
        (workspace.artifacts_dir / "review_feedback.json").write_text(
            json.dumps({"comments": [], "files": []}), encoding="utf-8"
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.HUMAN_MR_APPROVAL

    # -- reconcile DIVERGED ----------------------------------------------

    def test_reconcile_diverged_blocks(self, mixin, ticket, ctx, _patch_facade):
        """ReconcileResult.DIVERGED → BLOCKED."""
        _patch_facade.git_ops.ReconcileResult.DIVERGED = "DIVERGED"
        _patch_facade.git_ops.reconcile_with_remote_pr.return_value = "DIVERGED"
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.BLOCKED
        assert "diverged" in outcome.note.lower()

    # -- reconcile UNAVAILABLE (proceeds) --------------------------------

    def test_reconcile_unavailable_proceeds(self, mixin, ticket, ctx, _patch_facade):
        """UNAVAILABLE logs warning but continues to agent run."""
        _patch_facade.git_ops.ReconcileResult.UNAVAILABLE = "UNAVAILABLE"
        _patch_facade.git_ops.reconcile_with_remote_pr.return_value = "UNAVAILABLE"
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.HUMAN_MR_APPROVAL

    # -- agent success with changes --------------------------------------

    def test_agent_done_push_ok_routes_human_mr(
        self, mixin, ticket, ctx, _patch_facade
    ):
        """Agent DONE + different SHAs → push, reset counter, HUMAN_MR_APPROVAL."""
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.HUMAN_MR_APPROVAL
        _patch_facade.git_ops.push_with_lease.assert_called_once()

    def test_agent_done_resets_counter(
        self, mixin, ticket, ctx, workspace, _patch_facade
    ):
        """On success, counter is reset to 0."""
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        mixin._run_review_revision(ticket, ctx)
        counter_path = workspace.artifacts_dir / "review_revision_attempts.txt"
        assert counter_path.read_text(encoding="utf-8").strip() == "0"

    # -- agent DONE but no changes ---------------------------------------

    def test_agent_done_no_changes_retries(self, mixin, ticket, ctx, _patch_facade):
        """DONE but head==remote + under max → ADDRESSING_REVIEW."""
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha1"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.ADDRESSING_REVIEW

    def test_agent_done_no_changes_at_max_blocks(
        self, mixin, ticket, ctx, workspace, settings, _patch_facade
    ):
        """DONE, no changes, counter already at max → BLOCKED."""
        settings.review_revision_max_attempts = 1
        # Pre-set counter to 1 so attempt=2 > max=1.
        counter_path = workspace.artifacts_dir / "review_revision_attempts.txt"
        counter_path.write_text("1", encoding="utf-8")
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha1"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.BLOCKED

    # -- push failure ----------------------------------------------------

    def test_push_failure_blocks(self, mixin, ticket, ctx, _patch_facade):
        """push_with_lease raises → BLOCKED."""
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.git_ops.push_with_lease.side_effect = RuntimeError("denied")
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.BLOCKED
        assert "force-push failed" in outcome.note

    # -- head_sha / remote_branch_sha exception -------------------------

    def test_head_sha_exception_proceeds_push(self, mixin, ticket, ctx, _patch_facade):
        """head_sha raises → local=None, remote='force-push' → proceeds."""
        _patch_facade.git_ops.head_sha.side_effect = OSError("gone")
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.HUMAN_MR_APPROVAL
        _patch_facade.git_ops.push_with_lease.assert_called_once()

    # -- agent crash -----------------------------------------------------

    def test_agent_crash_retries(self, mixin, ticket, ctx, _patch_facade):
        """run_review_revision_agent raises → ADDRESSING_REVIEW (retry)."""
        _patch_facade.run_review_revision_agent.side_effect = RuntimeError("crash")
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.ADDRESSING_REVIEW

    def test_agent_crash_at_max_blocks(
        self, mixin, ticket, ctx, workspace, settings, _patch_facade
    ):
        """Agent crash + counter at max → BLOCKED."""
        settings.review_revision_max_attempts = 1
        counter_path = workspace.artifacts_dir / "review_revision_attempts.txt"
        counter_path.write_text("1", encoding="utf-8")
        _patch_facade.run_review_revision_agent.side_effect = RuntimeError("crash")
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.BLOCKED

    # -- agent not DONE --------------------------------------------------

    def test_agent_not_done_retries(self, mixin, ticket, ctx, _patch_facade):
        """Agent status != 'DONE' → ADDRESSING_REVIEW."""
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="FAILED", updated_memory=None
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.ADDRESSING_REVIEW

    def test_agent_not_done_at_max_blocks(
        self, mixin, ticket, ctx, workspace, settings, _patch_facade
    ):
        """Agent status != 'DONE' + counter at max → BLOCKED."""
        settings.review_revision_max_attempts = 1
        counter_path = workspace.artifacts_dir / "review_revision_attempts.txt"
        counter_path.write_text("1", encoding="utf-8")
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="FAILED", updated_memory=None
        )
        outcome = mixin._run_review_revision(ticket, ctx)
        assert outcome.next_state == State.BLOCKED

    # -- persisted memory ------------------------------------------------

    def test_agent_persists_updated_memory(
        self, mixin, ticket, ctx, _patch_facade, settings
    ):
        """updated_memory is passed to persist_memory."""
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory='{"key": "val"}'
        )
        mixin._run_review_revision(ticket, ctx)
        _patch_facade.persist_memory.assert_called_once()

    # -- comments formatting ---------------------------------------------

    def test_comments_formatted_correctly(
        self, mixin, ticket, ctx, workspace, _patch_facade
    ):
        """Agent is called with formatted review comments."""
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        mixin._run_review_revision(ticket, ctx)
        call_kwargs = _patch_facade.run_review_revision_agent.call_args[1]
        comments_text = call_kwargs["review_comments"]
        assert "## Comment #1 (a.py:10)" in comments_text
        assert "fix this" in comments_text
        assert "## Comment #2 (b.py)" in comments_text
        assert "also fix" in comments_text

    # -- tracing span ----------------------------------------------------

    def test_tracing_span_used(self, mixin, ticket, ctx, _patch_facade):
        """Agent runs inside a Langfuse root span."""
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        mixin._run_review_revision(ticket, ctx)
        _patch_facade.tracing.start_ticket_root_span.assert_called_once_with(
            ticket.id, "review_revision"
        )

    # -- comment without path --------------------------------------------

    def test_comment_without_path(self, mixin, ticket, ctx, workspace, _patch_facade):
        """Comment with no path field still renders."""
        (workspace.artifacts_dir / "review_feedback.json").write_text(
            json.dumps({"comments": [{"body": "general"}], "files": []}),
            encoding="utf-8",
        )
        _patch_facade.git_ops.head_sha.return_value = "sha1"
        _patch_facade.git_ops.remote_branch_sha.return_value = "sha2"
        _patch_facade.run_review_revision_agent.return_value = MagicMock(
            status="DONE", updated_memory=None
        )
        mixin._run_review_revision(ticket, ctx)
        call_kwargs = _patch_facade.run_review_revision_agent.call_args[1]
        comments_text = call_kwargs["review_comments"]
        assert "## Comment #1" in comments_text
        assert "general" in comments_text
        # No parenthesized path annotation.
        assert "(" not in comments_text.split("## Comment #1")[1].split("\n\n")[0]


# ========================================== _review_changes_requested_outcome


class TestReviewChangesRequestedOutcome:
    @pytest.fixture(autouse=True)
    def _setup_branch(self, ticket):
        ticket.branch = "feature/x"

    # -- feature flag off ------------------------------------------------

    def test_feature_flag_off_returns_none(self, mixin, ticket, ctx, settings, forge):
        """Disabled → None."""
        settings.review_feedback_enabled = False
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        assert result is None

    # -- pr_review_status exception --------------------------------------

    def test_pr_review_status_exception_returns_none(self, mixin, ticket, ctx, forge):
        """Transient forge error → None."""
        forge.pr_review_status.side_effect = ConnectionError("transient")
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        assert result is None

    # -- review_status is None -------------------------------------------

    def test_review_status_none_returns_none(self, mixin, ticket, ctx, forge):
        """None review status → None."""
        forge.pr_review_status.return_value = None
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        assert result is None

    # -- state != CHANGES_REQUESTED --------------------------------------

    def test_approved_state_returns_none(self, mixin, ticket, ctx, forge):
        """APPROVED state → None."""
        forge.pr_review_status.return_value = {
            "state": "APPROVED",
            "comments": [],
            "body": "",
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        assert result is None

    # -- empty comments + empty body -------------------------------------

    def test_empty_comments_empty_body_returns_none(self, mixin, ticket, ctx, forge):
        """CHANGES_REQUESTED with no actionable content → None."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [],
            "body": "",
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        assert result is None

    # -- empty comments + non-empty body (synthesize) --------------------

    def test_empty_comments_non_empty_body_synthesizes(self, mixin, ticket, ctx, forge):
        """CHANGES_REQUESTED with body but no comments → synthesize one."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [],
            "body": "Please fix the signature",
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        assert result is not None
        assert result.next_state == State.ADDRESSING_REVIEW
        assert "1 comment" in result.note

    # -- has comments → ADDRESSING_REVIEW --------------------------------

    def test_has_comments_routes_addressing_review(self, mixin, ticket, ctx, forge):
        """CHANGES_REQUESTED with comments → ADDRESSING_REVIEW."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [
                {"path": "a.py", "line": 10, "body": "fix this"},
            ],
            "body": "",
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        assert result is not None
        assert result.next_state == State.ADDRESSING_REVIEW

    # -- artifact persisted ----------------------------------------------

    def test_artifact_persisted(self, mixin, ticket, ctx, workspace, forge):
        """review_feedback.json is written to artifacts directory."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
        }
        mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        artifact = workspace.artifacts_dir / "review_feedback.json"
        assert artifact.exists()
        data = json.loads(artifact.read_text(encoding="utf-8"))
        assert data["state"] == "CHANGES_REQUESTED"

    # -- synthesized comment in artifact ---------------------------------

    def test_synthesized_comment_in_artifact(
        self, mixin, ticket, ctx, workspace, forge
    ):
        """Synthesized comment is written into the persisted artifact."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [],
            "body": "Please fix the signature",
        }
        mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge
        )
        artifact = workspace.artifacts_dir / "review_feedback.json"
        data = json.loads(artifact.read_text(encoding="utf-8"))
        assert len(data["comments"]) == 1
        assert data["comments"][0]["body"] == "Please fix the signature"
        assert data["comments"][0]["path"] == ""
        assert data["comments"][0]["line"] is None

    # -- stale review (commit_id != pr_head_sha) -------------------------

    def test_stale_review_discarded(self, mixin, ticket, ctx, forge):
        """CHANGES_REQUESTED review against old commit → dismissed + discarded (None)."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "abc111",
            "review_id": 42,
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        assert result is None
        forge.dismiss_review.assert_called_once_with(
            source_branch="feature/x", review_id=42
        )

    def test_stale_review_no_commit_id_passes_through(self, mixin, ticket, ctx, forge):
        """Empty commit_id in review (legacy) → not treated as stale."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "",
            "review_id": 42,
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        # Empty commit_id → no staleness check applied → routes normally
        assert result is not None
        assert result.next_state == State.ADDRESSING_REVIEW
        forge.dismiss_review.assert_not_called()

    def test_matching_commit_id_not_stale(self, mixin, ticket, ctx, forge):
        """Matching commit_id → not stale → routes ADDRESSING_REVIEW."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "abc123",
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc123"
        )
        assert result is not None
        assert result.next_state == State.ADDRESSING_REVIEW

    def test_empty_pr_head_sha_not_stale(self, mixin, ticket, ctx, forge):
        """Empty pr_head_sha (no PR data) → staleness check skipped."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "abc111",
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha=""
        )
        assert result is not None
        assert result.next_state == State.ADDRESSING_REVIEW

    # -- stale review dismissal when review_feedback_enabled is False -----

    def test_feedback_disabled_stale_review_dismissed(self, mixin, ticket, ctx, settings, forge):
        """When review_feedback_enabled=False, a stale CHANGES_REQUESTED
        review is dismissed on the forge and returns None."""
        settings.review_feedback_enabled = False
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "abc111",
            "review_id": 42,
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        assert result is None
        forge.dismiss_review.assert_called_once_with(
            source_branch="feature/x", review_id=42
        )

    def test_feedback_disabled_non_stale_not_dismissed(self, mixin, ticket, ctx, settings, forge):
        """When review_feedback_enabled=False and the review commit
        matches the PR head, dismiss_review is NOT called."""
        settings.review_feedback_enabled = False
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "abc123",
            "review_id": 42,
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc123"
        )
        assert result is None
        forge.dismiss_review.assert_not_called()

    def test_feedback_disabled_empty_commit_id_not_dismissed(self, mixin, ticket, ctx, settings, forge):
        """When review_feedback_enabled=False and review has no
        commit_id, dismiss_review is NOT called (can't determine staleness)."""
        settings.review_feedback_enabled = False
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "",
            "review_id": 42,
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        assert result is None
        forge.dismiss_review.assert_not_called()

    def test_feedback_disabled_not_changes_requested_returns_none(self, mixin, ticket, ctx, settings, forge):
        """When review_feedback_enabled=False and state is APPROVED,
        returns None without calling dismiss_review."""
        settings.review_feedback_enabled = False
        forge.pr_review_status.return_value = {
            "state": "APPROVED",
            "comments": [],
            "body": "",
            "commit_id": "abc111",
            "review_id": 42,
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        assert result is None
        forge.dismiss_review.assert_not_called()

    def test_feedback_disabled_review_status_none_returns_none(self, mixin, ticket, ctx, settings, forge):
        """When review_feedback_enabled=False and pr_review_status
        returns None, returns None without calling dismiss_review."""
        settings.review_feedback_enabled = False
        forge.pr_review_status.return_value = None
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        assert result is None
        forge.dismiss_review.assert_not_called()

    def test_feedback_disabled_pr_review_status_exception_returns_none(self, mixin, ticket, ctx, settings, forge):
        """When review_feedback_enabled=False and pr_review_status
        raises, returns None without crashing."""
        settings.review_feedback_enabled = False
        forge.pr_review_status.side_effect = ConnectionError("transient")
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        assert result is None

    # -- stale review dismissal when review_feedback_enabled is True ------

    def test_feedback_enabled_stale_review_dismissed(self, mixin, ticket, ctx, forge):
        """When review_feedback_enabled=True, a stale CHANGES_REQUESTED
        review is dismissed on the forge and returns None."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "abc111",
            "review_id": 42,
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        assert result is None
        forge.dismiss_review.assert_called_once_with(
            source_branch="feature/x", review_id=42
        )

    def test_feedback_enabled_stale_no_review_id_still_returns_none(self, mixin, ticket, ctx, forge):
        """When stale but review has no review_id, still returns None
        (dismiss_review is skipped gracefully)."""
        forge.pr_review_status.return_value = {
            "state": "CHANGES_REQUESTED",
            "comments": [{"path": "a.py", "line": 10, "body": "fix this"}],
            "body": "",
            "commit_id": "abc111",
            # no review_id key
        }
        result = mixin._review_changes_requested_outcome(
            ticket, ctx, branch="feature/x", forge=forge, pr_head_sha="abc222"
        )
        assert result is None
        forge.dismiss_review.assert_not_called()
