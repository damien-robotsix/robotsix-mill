"""Merge-stage core tests: fetch-before-rebase, rebase tracing, main-branch
CI debt detection, ``_is_pr_check_run`` and diverged-remote handling."""

import contextlib
import json
import subprocess

from robotsix_mill.agents.rebasing import RebaseResult
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.merge import MergeStage
from robotsix_mill.vcs.git_ops import PostPushResult, ReconcileResult
from tests.stages.merge.conftest import (
    _gh,
    _implement_complete,
    _in_rebasing,
)

# --- New: fetch-before-rebase-agent tests ---


def test_fetch_called_before_rebase_agent(tmp_path, monkeypatch):
    """git_ops.fetch is called once by reconcile_with_remote_pr for the PR
    branch before the agent runs. The target-branch fetch is now done by
    the agent via the git_fetch bridged tool."""
    ctx = _gh(tmp_path)
    calls = []

    def fake_fetch(repo, *, remote_url, token, branch):
        calls.append("fetch")

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        token_provider=None,
        token_cache_clear=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        calls.append("agent")
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        fake_fetch,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)
    # reconcile_with_remote_pr → fetch (PR branch), then agent.
    assert calls == ["fetch", "agent"]


def test_fetch_failure_does_not_invoke_agent(tmp_path, monkeypatch):
    """When reconcile fetch fails (UNAVAILABLE), the agent still runs —
    the stage only warns. The agent itself will call git_fetch and handle
    any fetch failures there."""

    ctx = _gh(tmp_path)
    agent_called = []

    def fake_fetch(repo, *, remote_url, token, branch):
        raise subprocess.CalledProcessError(1, "git fetch")

    def fake_rebase(
        *,
        settings,
        repo_dir,
        branch,
        target,
        memory="",
        remote_url=None,
        token=None,
        token_provider=None,
        token_cache_clear=None,
        pre_rebase_files=None,
        previously_dropped_files=None,
    ):
        agent_called.append(1)
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        fake_fetch,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": False,
        },
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    # Agent is still invoked — reconcile fetch failure is non-fatal.
    assert agent_called == [1]
    assert out.next_state is State.IMPLEMENT_COMPLETE


# --- tracing: root span only on first attempt ---


def test_root_span_only_on_first_rebase_attempt(tmp_path, monkeypatch):
    """start_ticket_root_span must fire only on attempt==1.
    Retries (attempt>1) skip the root span to avoid creating duplicate
    Langfuse traces for the same logical rebase operation."""
    import contextlib

    from robotsix_mill.runtime import tracing as tr

    ctx = _gh(tmp_path, rebase_max_attempts="3")

    root_calls = []
    stage_calls = []

    @contextlib.contextmanager
    def fake_root(ticket_id, stage_name, extra_attributes=None, repo_config=None):
        root_calls.append({"ticket_id": ticket_id, "stage_name": stage_name})
        yield

    @contextlib.contextmanager
    def fake_stage(stage_name):
        stage_calls.append(stage_name)
        yield

    # Capture real functions before patching to avoid recursion gotchas
    # if the wrapper were to import the real function after patching.
    _real_root = tr.start_ticket_root_span
    _real_stage = tr.trace_stage

    monkeypatch.setattr(tr, "start_ticket_root_span", fake_root)
    monkeypatch.setattr(tr, "trace_stage", fake_stage)
    # Agent always fails → stays REBASING (retry loop).
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="FAILED", summary="nope"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Run 3 times — simulating poll cycles.
    for _ in range(3):
        MergeStage().run(t, ctx)

    # Root span must have been called exactly once (first attempt only).
    assert len(root_calls) == 1, (
        f"expected 1 root span call, got {len(root_calls)}: {root_calls}"
    )
    assert root_calls[0]["ticket_id"] == t.id

    # trace_stage("rebase") called once per invocation.
    assert len(stage_calls) == 3, (
        f"expected 3 stage calls, got {len(stage_calls)}: {stage_calls}"
    )
    assert all(s == "rebase" for s in stage_calls)


# ============================================================
# Pre-existing main-branch CI debt detection
# ============================================================


def _run(
    workflow_id,
    name,
    conclusion,
    created_at,
    head_sha="abc",
    event="push",
    head_branch="feature",
):
    """Build a workflow-run dict as list_workflow_runs returns them."""
    return {
        "id": f"{workflow_id}-{created_at}",
        "name": name,
        "workflow_id": workflow_id,
        "head_sha": head_sha,
        "conclusion": conclusion,
        "html_url": "https://example/run",
        "created_at": created_at,
        "event": event,
        "head_branch": head_branch,
    }


def _patch_failing_pr(monkeypatch, sha="abc"):
    """PR open + mergeable with failing CI and a resolvable head SHA."""
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": sha,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "failure", "failing": []},
    )


def _patch_workflow_runs(monkeypatch, *, pr_runs, main_runs):
    """Route list_workflow_runs by which kwarg the caller passes."""

    def fake(self, *, branch=None, head_sha=None):
        if head_sha is not None:
            return pr_runs
        return main_runs

    monkeypatch.setattr(github.GitHubForge, "list_workflow_runs", fake)


def test_latest_failing_workflows_picks_most_recent_run():
    """Latest completed run per workflow_id wins (later green supersedes
    earlier red, and vice-versa)."""
    from robotsix_mill.stages.merge import _latest_failing_workflows

    runs = [
        # workflow 1: later run is green → not failing.
        _run(1, "tests", "failure", "2026-06-11T10:00:00Z"),
        _run(1, "tests", "success", "2026-06-11T11:00:00Z"),
        # workflow 2: later run is red → failing.
        _run(2, "lint", "success", "2026-06-11T10:00:00Z"),
        _run(2, "lint", "failure", "2026-06-11T11:00:00Z"),
    ]
    assert _latest_failing_workflows(runs) == {"lint"}


def test_latest_failing_workflows_ignores_in_progress_runs():
    """In-progress runs (conclusion=None) must NOT mask a completed failure.

    A newer in-progress run must not replace an older completed failure in the
    per-workflow "latest" map — otherwise a transient main-CI-in-flight window
    falsely hides a known failure and the pre-existing-debt check lets a PR
    through instead of blocking it."""
    from robotsix_mill.stages.merge import _latest_failing_workflows

    runs = [
        # Older completed failure.
        _run(1, "lint", "failure", "2026-06-11T10:00:00Z"),
        # Newer in-progress run — must NOT mask the failure above.
        {
            "id": "1-recent",
            "name": "lint",
            "workflow_id": 1,
            "head_sha": "abc",
            "conclusion": None,
            "html_url": "https://example/run",
            "created_at": "2026-06-11T11:00:00Z",
        },
    ]
    assert _latest_failing_workflows(runs) == {"lint"}


def test_implement_complete_blocks_on_shared_main_debt(tmp_path, monkeypatch):
    """Every PR-failing workflow is also failing on main → BLOCKED, reason
    names the workflow(s)."""
    ctx = _gh(tmp_path)
    _patch_failing_pr(monkeypatch)
    _patch_workflow_runs(
        monkeypatch,
        pr_runs=[_run(1, "lint", "failure", "2026-06-11T11:00:00Z")],
        main_runs=[_run(1, "lint", "failure", "2026-06-11T11:00:00Z")],
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "lint" in out.note


def test_implement_complete_pr_specific_failure_retries(tmp_path, monkeypatch):
    """A workflow failing on the PR but green on main is a genuine,
    PR-introduced failure → existing retry behaviour (FIXING_CI), not BLOCKED."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _patch_failing_pr(monkeypatch)
    _patch_workflow_runs(
        monkeypatch,
        pr_runs=[_run(1, "lint", "failure", "2026-06-11T11:00:00Z")],
        main_runs=[_run(1, "lint", "success", "2026-06-11T11:00:00Z")],
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI


def test_implement_complete_no_block_when_main_green(tmp_path, monkeypatch):
    """No failing workflows on main → unchanged behaviour (no BLOCKED)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _patch_failing_pr(monkeypatch)
    _patch_workflow_runs(
        monkeypatch,
        pr_runs=[_run(1, "lint", "failure", "2026-06-11T11:00:00Z")],
        main_runs=[_run(1, "lint", "success", "2026-06-11T11:00:00Z")],
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI


def test_implement_complete_no_sha_falls_through(tmp_path, monkeypatch):
    """PR head has no resolvable SHA → helper returns empty, normal retry."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    # PR with NO sha key + failing CI.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "failure", "failing": []},
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI


def test_implement_complete_list_workflow_runs_raises_falls_through(
    tmp_path, monkeypatch
):
    """list_workflow_runs raising → best-effort empty set, normal retry, no
    exception escapes."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _patch_failing_pr(monkeypatch)

    def boom(self, *, branch=None, head_sha=None):
        raise RuntimeError("forge down")

    monkeypatch.setattr(github.GitHubForge, "list_workflow_runs", boom)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI


def test_implement_complete_main_debt_detection_disabled(tmp_path, monkeypatch):
    """Flag off → even fully-shared debt does NOT block; prior behaviour."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path, auto_merge_main_debt_detection_enabled=False)
    _patch_failing_pr(monkeypatch)
    _patch_workflow_runs(
        monkeypatch,
        pr_runs=[_run(1, "lint", "failure", "2026-06-11T11:00:00Z")],
        main_runs=[_run(1, "lint", "failure", "2026-06-11T11:00:00Z")],
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI


# ---------------------------------------------------------------------------
# _is_pr_check_run unit tests
# ---------------------------------------------------------------------------


def test_is_pr_check_run_classification():
    """Verify _is_pr_check_run correctly classifies each trigger event."""
    from robotsix_mill.stages.merge import _is_pr_check_run

    # PR-check events → True.
    assert _is_pr_check_run({"event": "pull_request", "head_branch": "feature"}) is True
    assert (
        _is_pr_check_run({"event": "pull_request_target", "head_branch": "feat"})
        is True
    )
    assert _is_pr_check_run({"event": "merge_group", "head_branch": None}) is True

    # Branch push with non-empty head_branch → True.
    assert _is_pr_check_run({"event": "push", "head_branch": "main"}) is True
    assert _is_pr_check_run({"event": "push", "head_branch": "feature/x"}) is True

    # Tag push (head_branch is None or empty) → False.
    assert _is_pr_check_run({"event": "push", "head_branch": None}) is False
    assert _is_pr_check_run({"event": "push", "head_branch": ""}) is False
    assert _is_pr_check_run({"event": "push", "head_branch": "  "}) is False

    # Non-PR-check events → False.
    assert _is_pr_check_run({"event": "release"}) is False
    assert _is_pr_check_run({"event": "schedule"}) is False
    assert _is_pr_check_run({"event": "workflow_dispatch"}) is False

    # Missing event key → True (back-compat).
    assert _is_pr_check_run({"head_branch": "main"}) is True
    assert _is_pr_check_run({}) is True


def test_implement_complete_tag_release_not_blocked(tmp_path, monkeypatch):
    """Target only has a failing tag/release workflow (event=push, head_branch=None)
    → NOT blocked (the release run is excluded; debt set empty)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _patch_failing_pr(monkeypatch)
    _patch_workflow_runs(
        monkeypatch,
        pr_runs=[
            _run(
                1,
                "publish",
                "failure",
                "2026-06-11T11:00:00Z",
                event="push",
                head_branch=None,
            )
        ],
        main_runs=[
            _run(
                1,
                "publish",
                "failure",
                "2026-06-11T11:00:00Z",
                event="push",
                head_branch=None,
            )
        ],
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI  # falls through to normal CI path


def test_implement_complete_pr_check_debt_still_blocks(tmp_path, monkeypatch):
    """Failing PR-check workflows (push with branch, or pull_request) that also
    fail on main still block as before."""
    ctx = _gh(tmp_path)
    _patch_failing_pr(monkeypatch)
    _patch_workflow_runs(
        monkeypatch,
        pr_runs=[
            _run(
                1,
                "lint",
                "failure",
                "2026-06-11T11:00:00Z",
                event="push",
                head_branch="feature",
            )
        ],
        main_runs=[
            _run(
                1,
                "lint",
                "failure",
                "2026-06-11T11:00:00Z",
                event="push",
                head_branch="main",
            )
        ],
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "lint" in out.note


def test_implement_complete_schedule_workflow_not_blocked(tmp_path, monkeypatch):
    """A schedule-triggered workflow failure on both PR and main is excluded
    → not blocked."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _patch_failing_pr(monkeypatch)
    _patch_workflow_runs(
        monkeypatch,
        pr_runs=[
            _run(
                1,
                "nightly",
                "failure",
                "2026-06-11T11:00:00Z",
                event="schedule",
                head_branch=None,
            )
        ],
        main_runs=[
            _run(
                1,
                "nightly",
                "failure",
                "2026-06-11T11:00:00Z",
                event="schedule",
                head_branch=None,
            )
        ],
    )
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI  # falls through to normal CI path


# ============================================================
# Diverged remote PR branch → BLOCKED, never force-push
# (stage-level integration coverage for the lease-bypass data-loss guard)
# ============================================================


def test_run_review_revision_diverged_returns_blocked_and_skips_push(
    tmp_path, monkeypatch
):
    """When reconcile reports the PR branch DIVERGED, _run_review_revision must
    BLOCK and must NOT call post_push_check."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)

    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        merge_mod.git_ops,
        "reconcile_with_remote_pr",
        lambda *a, **k: ReconcileResult.DIVERGED,
    )
    monkeypatch.setattr(
        merge_mod,
        "run_review_revision_agent",
        lambda **k: type("_R", (), {"status": "DONE", "updated_memory": ""})(),
    )
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {"called": False}

    def _spy_push(*a, **k):
        pushed["called"] = True
        raise AssertionError("post_push_check must not run on a diverged branch")

    monkeypatch.setattr(merge_mod.git_ops, "post_push_check", _spy_push)

    t = _implement_complete(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    (repo_dir / ".git").mkdir(parents=True)
    # The revision agent only runs when there is review feedback to address.
    ctx.service.workspace(t).artifacts_dir.mkdir(parents=True, exist_ok=True)
    (ctx.service.workspace(t).artifacts_dir / "review_feedback.json").write_text(
        json.dumps({"comments": [{"body": "please fix"}], "files": []}),
        encoding="utf-8",
    )

    out = MergeStage()._run_review_revision(t, ctx)
    assert out.next_state is State.BLOCKED
    assert pushed["called"] is False
    assert "diverged" in (out.note or "").lower()


def test_fetch_and_run_rebase_diverged_returns_blocked_outcome(tmp_path, monkeypatch):
    """When reconcile reports the PR branch DIVERGED, _fetch_and_run_rebase
    returns an Outcome(BLOCKED) (not a bool) and never reaches a push.  This
    method returns bool | Outcome; assert the Outcome shape."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)

    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        merge_mod.git_ops,
        "reconcile_with_remote_pr",
        lambda *a, **k: ReconcileResult.DIVERGED,
    )
    monkeypatch.setattr(merge_mod.git_ops, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(
        merge_mod,
        "run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    pushed = {"called": False}

    def _spy_push(*a, **k):
        pushed["called"] = True
        raise AssertionError("post_push_check must not run on a diverged branch")

    monkeypatch.setattr(merge_mod.git_ops, "post_push_check", _spy_push)

    t = _in_rebasing(ctx)
    branch = f"mill/{t.id}"

    out = MergeStage()._fetch_and_run_rebase(
        t,
        ctx.settings,
        ctx.repo_config,
        "/repo",
        branch,
        "main",
        1,
    )
    assert isinstance(out, Outcome)
    assert out.next_state is State.BLOCKED
    assert pushed["called"] is False
    assert "diverged" in (out.note or "").lower()
