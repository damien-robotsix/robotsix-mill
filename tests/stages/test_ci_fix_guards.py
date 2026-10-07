"""Guard/gate CIFixStage tests split out of ``test_ci_fix``.

Covers the identical-failure gate, the staleness rebase-before-cycle ceiling,
the CodeQL alerts-unreadable (403) guard, and transient CI-failure auto-retry.
Shared helpers are imported from the parent ``test_ci_fix`` module; the autouse
``_mock_proactive_rebase_git_ops`` fixture is copied here verbatim because
module-level autouse fixtures do not apply across modules.
"""

import pytest

from robotsix_mill.agents.ci_fixing import CiFixResult
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages.ci_fix import (
    CIFixStage,
)
from robotsix_mill.stages.ci_fix_helpers import (
    _build_failing_summary,
    _ci_failure_fingerprint,
    _read_counter,
    _write_counter,
)
from robotsix_mill.vcs import git_ops

# ---------------------------------------------------------------------------
# Module-level autouse fixture: prevent any test from accidentally running
# real git fetch / push operations during the proactive-rebase step added
# in _resolve_clone_and_status.  Without this, git fetch against a fake
# forge URL hangs until the suite's 300s timeout, producing rc=124.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_proactive_rebase_git_ops(monkeypatch):
    """Stub every git operation in ci_fix that would touch the real network
    so no test ever hangs waiting for a remote.

    * ``try_rebase_onto`` → ``False`` (nothing to rebase)
    * ``push`` → no-op
    * ``head_sha`` → ``"abc123"`` (fake commit SHA)
    * ``ls_remote_sha`` → ``None`` (no remote branch — skips empty-commit path)
    * ``empty_commit`` → no-op
    * ``reconcile_with_remote_pr`` → ``SYNCED`` (no foreign commits)
    * ``post_push_check`` → ``PASS`` (push landed cleanly)
    * ``try_rebase_onto_branch`` → ``True`` (auto-retry rebase succeeds)
    * ``push_with_lease`` → no-op (auto-retry re-push)

    Tests that need different behaviour override the relevant stub with
    their own ``monkeypatch.setattr`` — call-verification assertions (e.g.
    counting calls) continue to work because ``monkeypatch`` is function-
    scoped and later ``setattr`` calls simply overwrite the earlier stub.
    """
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "abc123",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.ls_remote_sha",
        lambda remote_url, ref, token=None: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.empty_commit",
        lambda repo, message: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.reconcile_with_remote_pr",
        lambda repo, remote_url, branch, token: git_ops.ReconcileResult.SYNCED,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )
    # Auto-retry path (NOT_LANDED recovery): rebase succeeds, re-push no-op.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto_branch",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )


from tests.stages.test_ci_fix import (
    _failing_check_status,
    _fixing_ci,
    _flip_check_status_to_success_after_first,
    _gh,
    _oos_forge,
    _oos_result,
    _setup_repo,
)

# ---
# Identical-failure gate
# ---


def test_identical_failure_blocks_after_max_consecutive(tmp_path, monkeypatch):
    """When the same CI failure fingerprint repeats ci_fix_max_identical_failures
    times, the second occurrence returns BLOCKED without invoking the agent."""
    ctx = _gh(tmp_path, ci_fix_max_identical_failures="2")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )

    agent_calls = []

    def fake_agent(**k):
        agent_calls.append(1)
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        fake_agent,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    # Compute the current failure fingerprint and pre-seed the fingerprint file.
    repo_id = ctx.repo_config.board_id
    failing = [{"name": "lint", "summary": "err", "text": None, "annotations": []}]
    summary = _build_failing_summary(failing)
    fp = _ci_failure_fingerprint(summary, repo_id, head_sha="abc123")
    artifacts = ctx.service.workspace(t).artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "ci_failure_fingerprint.txt").write_text(fp, encoding="utf-8")

    counter_path = artifacts / "ci_identical_failure_count.txt"
    assert not counter_path.exists()

    # First run: fingerprint matches → counter increments to 1, agent runs.
    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.IMPLEMENT_COMPLETE
    assert agent_calls == [1]
    assert counter_path.read_text(encoding="utf-8").strip() == "1"

    # Second run: same fingerprint → counter increments to 2 → BLOCKED.
    out2 = CIFixStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert fp in out2.note
    # Agent was NOT called on the second run.
    assert agent_calls == [1]
    assert counter_path.read_text(encoding="utf-8").strip() == "2"


def test_identical_failure_resets_on_changed_fingerprint(tmp_path, monkeypatch):
    """When the CI failure fingerprint changes, the counter resets to 0
    and the fingerprint file is updated to the new fingerprint."""
    ctx = _gh(tmp_path, ci_fix_max_identical_failures="2")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "new err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )

    agent_calls = []

    def fake_agent(**k):
        agent_calls.append(1)
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        fake_agent,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    repo_id = ctx.repo_config.board_id
    artifacts = ctx.service.workspace(t).artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)

    # Pre-seed the counter at 5 (simulating prior consecutive failures).
    counter_path = artifacts / "ci_identical_failure_count.txt"
    _write_counter(counter_path, 5)

    # Pre-seed a DIFFERENT fingerprint (different check name).
    old_summary = _build_failing_summary(
        [{"name": "pytest", "summary": "old", "text": None, "annotations": []}]
    )
    old_fp = _ci_failure_fingerprint(old_summary, repo_id, head_sha="abc123")
    (artifacts / "ci_failure_fingerprint.txt").write_text(old_fp, encoding="utf-8")

    # Current failure is "lint" (different from "pytest" in the stored FP).
    failing = [{"name": "lint", "summary": "new err", "text": None, "annotations": []}]
    current_summary = _build_failing_summary(failing)
    current_fp = _ci_failure_fingerprint(current_summary, repo_id, head_sha="abc123")
    assert current_fp != old_fp  # fingerprints must differ for this test

    # Run the stage → fingerprint changed → counter resets, agent runs.
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert agent_calls == [1]

    # Counter was reset to 0.
    assert _read_counter(counter_path) == 0

    # Fingerprint file was updated to the current fingerprint.
    stored = (
        (artifacts / "ci_failure_fingerprint.txt").read_text(encoding="utf-8").strip()
    )
    assert stored == current_fp


def test_transient_fingerprint_reruns_instead_of_blocking(tmp_path, monkeypatch):
    """A repeated fingerprint classified as a transient external 5xx must NOT
    escalate to a hard BLOCK — the stage re-runs the workflow and re-polls
    (auto-retry with backoff) instead."""
    ctx = _gh(
        tmp_path,
        ci_fix_max_identical_failures="2",
        ci_transient_max_retries="3",
    )
    failing = [
        {
            "name": "js-lint",
            "summary": "npm error 503 Service Unavailable - GET "
            "https://registry.npmjs.org/-/npm/v1/security/audits",
            "text": None,
            "annotations": [],
        }
    ]
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": failing,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    # No failing runs surfaced during resolve (keeps the fingerprint free of
    # appended job-log text); the gate's own re-run helper is exercised by the
    # dedicated _rerun_failing_workflows tests below.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: [],
    )

    def fake_agent(**k):
        raise AssertionError("agent must not run for a transient repeat")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        fake_agent,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    repo_id = ctx.repo_config.board_id
    summary = _build_failing_summary(failing)
    fp = _ci_failure_fingerprint(summary, repo_id, head_sha="abc123")
    artifacts = ctx.service.workspace(t).artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "ci_failure_fingerprint.txt").write_text(fp, encoding="utf-8")

    counter_path = artifacts / "ci_identical_failure_count.txt"
    _write_counter(counter_path, 1)
    # Exhaust the quick transient re-run budget so _resolve_clone_and_status
    # does not re-trigger first — force the flow through the fingerprint gate.
    _write_counter(artifacts / "ci_transient_retry.txt", 3)

    out = CIFixStage().run(t, ctx)
    # Auto-retry (re-poll) rather than a hard human gate.
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # Identical-failure counter reset so the ticket keeps auto-retrying.
    assert counter_path.read_text(encoding="utf-8").strip() == "0"


def test_rerun_failing_workflows_reruns_each_failed_run(tmp_path, monkeypatch):
    """_rerun_failing_workflows re-queues every failing run at head_sha and
    returns the count of runs re-triggered."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: [
            {"id": 1, "conclusion": "failure"},
            {"id": 2, "conclusion": "success"},
            {"id": 3, "conclusion": "failure"},
        ],
    )
    rerun_calls: list[int] = []
    monkeypatch.setattr(
        github.GitHubForge,
        "rerun_workflow",
        lambda self, *, run_id: rerun_calls.append(run_id) or {"rerun": True},
    )
    t = _fixing_ci(ctx)
    reran = CIFixStage()._rerun_failing_workflows(t, ctx, "abc123")
    assert reran == 2
    assert rerun_calls == [1, 3]


def test_rerun_failing_workflows_no_head_sha_is_noop(tmp_path, monkeypatch):
    """Without a head SHA there is nothing to re-run — the forge is untouched."""
    ctx = _gh(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: called.append(1) or [],
    )
    t = _fixing_ci(ctx)
    assert CIFixStage()._rerun_failing_workflows(t, ctx, "") == 0
    assert called == []


# ---------------------------------------------------------------------------
# Staleness guard: rebase before cycle ceiling
# ---------------------------------------------------------------------------


def test_stale_branch_rebase_skip_on_missing_clone(tmp_path, monkeypatch):
    """When the workspace clone is missing, _resolve_clone_and_status returns
    BLOCKED before _rebase_if_stale is ever reached — branch_is_behind_main is
    never called (it would crash on a non-existent repo dir)."""
    ctx = _gh(tmp_path)
    behind_calls = []

    def fake_behind(repo, target_branch):
        behind_calls.append(1)
        raise AssertionError("should never be called — clone is missing")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.branch_is_behind_main",
        fake_behind,
    )

    t = _fixing_ci(ctx)
    # No _setup_repo — clone is deliberately missing.

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "workspace clone is missing" in out.note
    # _rebase_if_stale was never reached → branch_is_behind_main never called.
    assert behind_calls == []


def test_agent_failed_blocks_immediately(tmp_path, monkeypatch):
    """A FAILED verdict (agent spent its iteration budget) → BLOCKED in one
    shot; there is no per-poll retry."""
    ctx = _gh(tmp_path)
    _failing_check_status(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="FAILED", summary="could not fix ruff"),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "iteration budget" in out.note


def test_agent_failed_ci_recovered_returns_implement_complete(tmp_path, monkeypatch):
    """A FAILED verdict (iteration budget spent) does NOT block when the agent
    already pushed a fix that turned CI green — the pre-block re-check sees
    'success' and returns to the merge poll instead."""
    ctx = _gh(tmp_path)
    calls = _flip_check_status_to_success_after_first(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="FAILED", summary="could not fix ruff"),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # The re-check queried the forge a second time before deciding.
    assert calls["n"] >= 2


def test_agent_timeout_ci_recovered_returns_implement_complete(tmp_path, monkeypatch):
    """A wall-clock timeout does NOT block when CI is green at block time — the
    pre-block re-check sees 'success' and returns to the merge poll."""
    ctx = _gh(tmp_path, ci_fix_agent_timeout_seconds="1800")
    calls = _flip_check_status_to_success_after_first(monkeypatch)

    def fake_invoke(self, ticket, ctx, repo_dir, branch, failing_summary):
        self._last_agent_timed_out = True
        self._last_agent_timeout_elapsed = 1850.0

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.CIFixStage._invoke_agent",
        fake_invoke,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert calls["n"] >= 2


def test_codeql_security_severity_block_note(tmp_path, monkeypatch):
    """A CodeQL-only failure with a security-severity alert produces a BLOCKED
    note that names the alert and states human sign-off is required, without
    the generic 'iteration budget' wording."""
    ctx = _gh(tmp_path)
    # Failing check is CodeQL
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "CodeQL / Analyze (python)",
                    "summary": "alert",
                    "text": None,
                    "annotations": [],
                }
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    # Return a security-severity alert (high).
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [
            {
                "number": 42,
                "rule": "py/clear-text-logging-sensitive-data",
                "security_severity_level": "high",
                "severity": "error",
                "path": "src/foo.py",
                "line": 10,
                "message": "Sensitive data logged",
            }
        ],
    )
    # The alert's file is in the PR's diff.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [
            {
                "path": "src/foo.py",
                "status": "modified",
                "additions": 1,
                "deletions": 0,
            }
        ],
    )
    # No failed workflow runs (no job logs needed).
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, head_sha=None, branch=None: [],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="FAILED", summary="could not fix"),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "CodeQL" in out.note
    assert "py/clear-text-logging-sensitive-data" in out.note
    assert "42" in out.note
    assert "security" in out.note.lower()
    assert "human sign-off" in out.note.lower()
    assert "iteration budget" not in out.note


def test_agent_crash_blocks(tmp_path, monkeypatch):
    """An agent crash (run_ci_fix_agent raises → _invoke_agent returns None)
    is treated as FAILED → BLOCKED."""
    ctx = _gh(tmp_path)
    _failing_check_status(monkeypatch)

    def boom(**k):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", boom)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED


def test_ci_status_fn_not_passed_to_agent(tmp_path, monkeypatch):
    """The stage does NOT wire ci_status_fn into the agent — the agent is
    one-shot (fix + push, no CI waiting)."""
    ctx = _gh(tmp_path)
    _failing_check_status(monkeypatch)
    captured = {}

    def fake_agent(**k):
        captured["ci_status_fn"] = k.get("ci_status_fn")
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", fake_agent)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # ci_status_fn is no longer passed — the agent is one-shot.
    assert captured["ci_status_fn"] is None


def test_make_ci_status_fn_maps_conclusions(tmp_path, monkeypatch):
    """_make_ci_status_fn returns (conclusion, summary) tuples matching the
    forge's check_status verdicts."""
    import time as _time_mod

    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    branch = f"mill/{t.id}"
    stage = CIFixStage()

    # Ensure the 120 s grace period is always expired so verdicts are
    # mapped straight through (the grace-period behaviour is tested
    # separately below).
    _tick = [0]

    def _fake_monotonic():
        _tick[0] += 1000.0
        return _tick[0]

    monkeypatch.setattr(_time_mod, "monotonic", _fake_monotonic)

    # Helper that accepts the new ``require_checks`` kwarg.
    def _cs(conclusion, failing=(), sha=""):
        def _fn(self, *, source_branch, require_checks=False):
            result: dict = {"conclusion": conclusion, "failing": list(failing)}
            if sha:
                result["_sha"] = sha
            return result

        return _fn

    # success
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        _cs("success", sha="abc1234"),
    )
    conclusion, summary = stage._make_ci_status_fn(t, ctx, branch)()
    assert conclusion == "success"
    assert "abc1234" in summary

    # pending
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        _cs("pending"),
    )
    assert stage._make_ci_status_fn(t, ctx, branch)() == ("pending", "")

    # gone (PR vanished)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: None,
    )
    assert stage._make_ci_status_fn(t, ctx, branch)() == ("gone", "")

    # failure carries a summary
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        _cs(
            "failure",
            failing=[
                {"name": "lint", "summary": "boom", "text": None, "annotations": []}
            ],
            sha="abc123",
        ),
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    conclusion, summary = stage._make_ci_status_fn(t, ctx, branch)()
    assert conclusion == "failure"
    assert "lint" in summary
    assert "abc123" in summary


def test_transient_check_status_error_maps_to_pending(tmp_path, monkeypatch):
    """A forge exception during the wait probe maps to 'pending' so the agent
    keeps waiting rather than giving up on a blip."""
    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    def boom(self, *, source_branch, require_checks=False):
        raise RuntimeError("forge 500")

    monkeypatch.setattr(github.GitHubForge, "check_status", boom)
    assert CIFixStage()._make_ci_status_fn(t, ctx, f"mill/{t.id}")() == ("pending", "")


def test_make_ci_status_fn_includes_run_id_in_failure_prefix(tmp_path, monkeypatch):
    """When failing workflow runs exist for the SHA, the run_id is included
    in the CI_FAILING prefix so the agent can pass it to fetch_ci_logs."""
    import time as _time_mod

    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    branch = f"mill/{t.id}"
    stage = CIFixStage()

    _tick = [0]

    def _fake_monotonic():
        _tick[0] += 1000.0
        return _tick[0]

    monkeypatch.setattr(_time_mod, "monotonic", _fake_monotonic)

    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "boom", "text": None, "annotations": []}
            ],
            "_sha": "abc123",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, head_sha, branch=None: [
            {
                "id": 30399400001,
                "name": "CI",
                "conclusion": "failure",
            }
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, *, run_id, full_log=False: "some log output",
    )

    conclusion, summary = stage._make_ci_status_fn(t, ctx, branch)()
    assert conclusion == "failure"
    assert "[sha: abc123, run: 30399400001]" in summary
    assert "lint" in summary


def test_failure_detail_caps_job_log_context(tmp_path, monkeypatch):
    """_build_failure_detail caps the inline job-log context (head+tail
    window) so late wait_for_ci iterations don't re-send unbounded log
    history, while preserving the first-error window (head) and the tail."""
    import time as _time_mod

    ctx = _gh(tmp_path, ci_fix_log_context_max_chars=500)
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    branch = f"mill/{t.id}"
    stage = CIFixStage()

    _tick = [0]

    def _fake_monotonic():
        _tick[0] += 1000.0
        return _tick[0]

    monkeypatch.setattr(_time_mod, "monotonic", _fake_monotonic)

    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "boom", "text": None, "annotations": []}
            ],
            "_sha": "abc123",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, head_sha, branch=None: [
            {"id": 30399400001, "name": "CI", "conclusion": "failure"}
        ],
    )
    big_log = "HEAD_MARKER_XYZ\n" + ("filler line\n" * 400) + "TAIL_MARKER_XYZ\n"
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, *, run_id, full_log=False: big_log,
    )

    conclusion, summary = stage._make_ci_status_fn(t, ctx, branch)()
    assert conclusion == "failure"
    assert "[... job logs truncated:" in summary
    assert "HEAD_MARKER_XYZ" in summary  # first-error window is preserved
    assert "TAIL_MARKER_XYZ" in summary  # recent tail is preserved


def test_make_ci_status_fn_compacts_late_iterations(tmp_path, monkeypatch):
    """On attempt >= 2 the status_fn returns a compact digest instead of the
    full failure detail, bounding per-iteration transcript growth."""
    import time as _time_mod

    ctx = _gh(
        tmp_path,
        ci_fix_log_context_max_chars=16000,
        ci_fix_iteration_summary_max_chars=1200,
    )
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    branch = f"mill/{t.id}"
    stage = CIFixStage()

    _tick = [0]

    def _fake_monotonic():
        _tick[0] += 1000.0
        return _tick[0]

    monkeypatch.setattr(_time_mod, "monotonic", _fake_monotonic)

    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "lint",
                    "summary": "boom",
                    "text": None,
                    "annotations": [
                        {
                            "path": "src/foo.py",
                            "start_line": 10,
                            "level": "failure",
                            "message": "unused import os",
                        }
                    ],
                }
            ],
            "_sha": "abc123",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, head_sha, branch=None: [
            {"id": 30399400001, "name": "CI", "conclusion": "failure"}
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, *, run_id, full_log=False: "some log output\n",
    )

    status_fn = stage._make_ci_status_fn(t, ctx, branch)

    _, full = status_fn(1)
    assert "**Job logs:**" in full  # attempt 1 keeps the full inline detail

    _, compact = status_fn(2)
    assert "compact summary" in compact
    assert "**Job logs:**" not in compact
    assert len(compact) <= 1200
    assert "lint" in compact
    assert "unused import os" in compact  # key error signature survives


def test_branch_own_failure_goes_straight_to_agent(tmp_path, monkeypatch):
    """A branch-own CI failure rebases onto main first, then runs the
    ci-fix agent on the first cycle — the rebase ensures a fresh CI run
    against current main so the failure fingerprint is never stale."""
    ctx = _gh(tmp_path)
    _failing_check_status(monkeypatch)
    rebase_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto",
        lambda *a, **k: rebase_calls.append(1) or True,
    )
    push_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: push_calls.append(1),
    )

    agent_calls = []

    def fake_agent(**k):
        agent_calls.append(1)
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", fake_agent)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert agent_calls == [1], "agent must run on the first cycle"
    assert rebase_calls == [1], "must rebase onto main before scanning CI"
    assert push_calls == [1], "must push after rebase"


# ---------------------------------------------------------------------------
# CodeQL alerts-unreadable (403) guard
# ---------------------------------------------------------------------------


def test_codeql_403_unreadable_blocks_immediately(tmp_path, monkeypatch):
    """When CodeQL is failing and list_code_scanning_alerts raises
    CodeScanningAlertsUnavailable (403), the stage blocks immediately with
    a permission-hint note and does NOT invoke the ci-fix agent."""
    from robotsix_mill.forge.github_code_scanning import (
        CodeScanningAlertsUnavailable,
    )

    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "CodeQL / Analyze (python)",
                    "summary": "alert",
                    "text": None,
                    "annotations": [],
                }
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    # list_code_scanning_alerts raises the 403 signal.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: (_ for _ in ()).throw(
            CodeScanningAlertsUnavailable("403 forbidden")
        ),
    )

    agent_called = []

    def fake_agent(**k):
        agent_called.append(True)
        return CiFixResult(status="DONE", summary="should not run")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", fake_agent)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "UNREADABLE" in out.note
    assert "security-events" in out.note
    assert "Code scanning alerts: read" in out.note
    assert not agent_called, "ci-fix agent must not be called on 403"


def test_codeql_403_readable_alerts_still_works(tmp_path, monkeypatch):
    """Readable CodeQL alerts → existing dismiss/unblock flow stays green
    (regression guard: the new 403 guard must not break the normal path)."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "CodeQL / Analyze (python)",
                    "summary": "alert",
                    "text": None,
                    "annotations": [],
                }
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    # Return a security-severity alert (high) — the normal readable path.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [
            {
                "number": 42,
                "rule": "py/clear-text-logging-sensitive-data",
                "security_severity_level": "high",
                "severity": "error",
                "path": "src/foo.py",
                "line": 10,
                "message": "Sensitive data logged",
            }
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [
            {"path": "src/foo.py", "status": "modified", "additions": 1, "deletions": 0}
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, head_sha=None, branch=None: [],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="FAILED", summary="could not fix"),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    # The block note references the real alert, not the 403 permission text.
    assert "42" in out.note
    assert "py/clear-text-logging-sensitive-data" in out.note
    assert "UNREADABLE" not in out.note


# ---------------------------------------------------------------------------
# Transient CI failure auto-retry (before spawning dependency fix)
# ---------------------------------------------------------------------------


def test_transient_econnreset_triggers_rerun_not_spawn(tmp_path, monkeypatch):
    """An ECONNRESET-classified OUT_OF_SCOPE triggers a workflow re-run
    (rerun_workflow) and returns IMPLEMENT_COMPLETE instead of spawning a
    blocking ci_fix_dependency ticket."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "CodeQL",
                    "summary": "CodeQL analysis failed",
                    "text": None,
                    "annotations": [],
                }
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [],
    )
    # list_workflow_runs returns one failing run with id 42.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: [
            {
                "id": 42,
                "name": "CodeQL",
                "workflow_id": 1,
                "head_sha": "abc123",
                "conclusion": "failure",
                "html_url": "",
                "created_at": "",
                "event": "push",
                "head_branch": "test-branch",
                "path": "",
            }
        ],
    )
    # Job logs must contain the transient signature so the classifier
    # detects it in the failing_summary built by _build_failure_detail.
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, *, run_id, full_log=False: (
            "Run github/codeql-action/analyze@v3\nError: ECONNRESET\n"
        ),
    )
    rerun_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "rerun_workflow",
        lambda self, *, run_id: rerun_calls.append(run_id) or {"rerun": True},
    )

    # The ci_fix agent returns OUT_OF_SCOPE.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(
            status="OUT_OF_SCOPE",
            summary="transient — CodeQL ECONNRESET",
            out_of_scope_reason="CodeQL analysis failed with ECONNRESET",
            failing_check="CodeQL",
            required_change_area="CodeQL analysis",
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    # Should return IMPLEMENT_COMPLETE (re-poll CI), not BLOCKED.
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # rerun_workflow should have been called with run_id=42.
    assert rerun_calls == [42]
    # No dependency fix ticket should have been spawned.
    assert ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY) == []


def test_transient_retry_exhausted_falls_through_to_spawn(tmp_path, monkeypatch):
    """When transient retries are exhausted (ci_transient_max_retries), the
    failure falls through to spawning a ci_fix_dependency ticket."""
    ctx = _gh(
        tmp_path,
        ci_transient_max_retries=0,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "CodeQL",
                    "summary": "CodeQL analysis failed",
                    "text": None,
                    "annotations": [],
                }
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [],
    )
    # list_workflow_runs returns one failing run with id 42.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: [
            {
                "id": 42,
                "name": "CodeQL",
                "workflow_id": 1,
                "head_sha": "abc123",
                "conclusion": "failure",
                "html_url": "",
                "created_at": "",
                "event": "push",
                "head_branch": "test-branch",
                "path": "",
            }
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, *, run_id, full_log=False: (
            "Run github/codeql-action/analyze@v3\nError: ECONNRESET\n"
        ),
    )
    rerun_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "rerun_workflow",
        lambda self, *, run_id: rerun_calls.append(run_id) or {"rerun": True},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(
            status="OUT_OF_SCOPE",
            summary="transient — CodeQL ECONNRESET",
            out_of_scope_reason="CodeQL analysis failed with ECONNRESET",
            failing_check="CodeQL",
            required_change_area="CodeQL analysis",
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    # Should fall through to BLOCKED (spawn dependency fix).
    assert out.next_state is State.BLOCKED
    # rerun_workflow should NOT have been called (retries=0).
    assert rerun_calls == []
    # A dependency fix ticket should have been spawned.
    fixes = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)
    assert len(fixes) == 1


def test_deterministic_failure_still_spawns_fix_ticket(tmp_path, monkeypatch):
    """A deterministic failure (e.g. ruff lint error) should still spawn a
    ci_fix_dependency ticket, bypassing the transient auto-retry path."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    rerun_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "rerun_workflow",
        lambda self, *, run_id: rerun_calls.append(run_id) or {"rerun": True},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    # rerun_workflow should NOT have been called (deterministic failure).
    assert rerun_calls == []
    fixes = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)
    assert len(fixes) == 1
