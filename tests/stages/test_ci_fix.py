"""Tests for the CIFixStage (FIXING_CI → IMPLEMENT_COMPLETE | BLOCKED)."""

import json

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.ci_fix import (
    CIFixStage,
    _read_counter,
    _write_counter,
    _build_failing_summary,
    _partition_alerts_by_diff,
)
from robotsix_mill.agents.ci_fixing import CiFixResult


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**env)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.get("FORGE_TOKEN")
    if ft is not None:
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg

        _reset_secrets()
        _cfg._secrets = Secrets(forge_token=ft)
    db.init_db(s, board_id="test-board")
    from robotsix_mill.config import RepoConfig

    return StageContext(
        settings=s,
        service=TicketService(s, board_id="test-board"),
        repo_config=RepoConfig(
            repo_id="test-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
        ),
    )


def _fixing_ci(ctx):
    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.FIXING_CI,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _gh(tmp_path, **extra):
    return _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        **extra,
    )


def _setup_repo(ctx, ticket):
    """Create a minimal .git in the workspace so _workspace_repo_dir succeeds."""
    repo_dir = ctx.service.workspace(ticket).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)
    return str(repo_dir)


# --- Fix success + push success → IMPLEMENT_COMPLETE ---


def test_fix_success_push_success_returns_implement_complete(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    # pr_status is called to get head_sha for job-log fetching.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    push_seen = {}

    def fake_push(repo, branch, remote_url, token):
        push_seen.update(branch=branch, token=token)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        fake_push,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert push_seen["branch"] == f"mill/{t.id}"

    # Counter reset to 0.
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_attempts.txt"
    assert _read_counter(counter) == 0


# --- Fix succeeds but makes no code changes → no-change counter → BLOCKED ---


def test_fix_success_no_change_hits_ceiling_blocks(tmp_path, monkeypatch):
    """When the ci-fix agent succeeds but produces no commits (local HEAD
    matches remote) for ci_max_auto_retries consecutive cycles, escalate
    to BLOCKED."""
    ctx = _gh(tmp_path, ci_max_auto_retries="2")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    push_seen = []

    def fake_push(repo, branch, remote_url, token):
        push_seen.append(branch)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        fake_push,
    )
    # Simulate no-change: local HEAD == remote HEAD.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "abc123",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.remote_branch_sha",
        lambda repo, branch: "abc123",
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    no_change_path = ctx.service.workspace(t).artifacts_dir / "ci_no_change_cycles.txt"

    # Cycle 1: no change → IMPLEMENT_COMPLETE, counter=1.
    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.IMPLEMENT_COMPLETE
    assert _read_counter(no_change_path) == 1
    assert len(push_seen) == 1

    # Cycle 2: no change again → hits ceiling (max=2) → BLOCKED.
    out2 = CIFixStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert "no code changes" in out2.note
    assert "infrastructure flakes" in out2.note
    # Counters reset on block.
    assert _read_counter(no_change_path) == 0


def test_fix_success_with_changes_resets_no_change_counter(tmp_path, monkeypatch):
    """When the ci-fix agent produces a real commit, the no-change counter resets."""
    ctx = _gh(tmp_path, ci_max_auto_retries="2")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    push_seen = []

    def fake_push(repo, branch, remote_url, token):
        push_seen.append(branch)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        fake_push,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    no_change_path = ctx.service.workspace(t).artifacts_dir / "ci_no_change_cycles.txt"

    # Cycle 1: no change (head == remote).
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "abc123",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.remote_branch_sha",
        lambda repo, branch: "abc123",
    )
    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.IMPLEMENT_COMPLETE
    assert _read_counter(no_change_path) == 1

    # Cycle 2: real change (head != remote).
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "def456",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.remote_branch_sha",
        lambda repo, branch: "abc123",
    )
    out2 = CIFixStage().run(t, ctx)
    assert out2.next_state is State.IMPLEMENT_COMPLETE
    # No-change counter reset to 0.
    assert _read_counter(no_change_path) == 0

    # Cycle 3: no change again → counter=1 (not blocked yet).
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "def456",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.remote_branch_sha",
        lambda repo, branch: "def456",
    )
    out3 = CIFixStage().run(t, ctx)
    assert out3.next_state is State.IMPLEMENT_COMPLETE
    assert _read_counter(no_change_path) == 1


def test_max_auto_retries_zero_disables_ceiling(tmp_path, monkeypatch):
    """When ci_max_auto_retries=0, the no-change ceiling is disabled
    (preserves pre-ceiling behaviour)."""
    ctx = _gh(tmp_path, ci_max_auto_retries="0", ci_fix_max_cycles="0")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    push_seen = []

    def fake_push(repo, branch, remote_url, token):
        push_seen.append(branch)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        fake_push,
    )
    # Simulate no-change: local HEAD == remote HEAD.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "abc123",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.remote_branch_sha",
        lambda repo, branch: "abc123",
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    no_change_path = ctx.service.workspace(t).artifacts_dir / "ci_no_change_cycles.txt"

    # Run 5 no-change cycles — none should block (ceiling disabled).
    for _ in range(5):
        out = CIFixStage().run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE
    # Counter still increments but never triggers a block.
    assert _read_counter(no_change_path) == 5
    assert len(push_seen) == 5


# --- Hard cycle ceiling bounds a churn-commit loop ---


def test_churn_loop_bounded_by_max_cycles(tmp_path, monkeypatch):
    """A churn loop (agent reports DONE + produces a commit every cycle while
    CI stays red) resets both pre-existing counters each cycle, so only the
    new hard ceiling can bound it.  After ci_fix_max_cycles cycles the stage
    blocks WITHOUT running the agent."""
    ctx = _gh(tmp_path, ci_fix_max_cycles="3")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    agent_calls = {"n": 0}

    def fake_agent(**k):
        agent_calls["n"] += 1
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        fake_agent,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )
    # Simulate a fresh churn commit every cycle: local != remote, so both
    # the attempt counter and no-change counter reset each cycle.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "local-sha",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.remote_branch_sha",
        lambda repo, branch: "remote-sha",
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"

    # Cycles 1-3 run the agent → IMPLEMENT_COMPLETE.
    for expected in (1, 2, 3):
        out = CIFixStage().run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE
        assert _read_counter(cycle_path) == expected
    assert agent_calls["n"] == 3

    # Cycle 4 reaches the ceiling → BLOCKED without running the agent.
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "hard ceiling of 3 cycle(s)" in out.note
    # Agent NOT invoked on the blocking cycle.
    assert agent_calls["n"] == 3
    # Cycle counter reset to 0 on the blocking return.
    assert _read_counter(cycle_path) == 0


def test_cycle_counter_resets_on_ci_green(tmp_path, monkeypatch):
    """A few failing cycles bump the cycle counter; once CI is observed green
    the counter resets to 0."""
    ctx = _gh(tmp_path, ci_fix_max_cycles="8")
    state = {"conclusion": "failure"}
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": state["conclusion"],
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "local-sha",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.remote_branch_sha",
        lambda repo, branch: "remote-sha",
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"

    # Two failing cycles bump the counter.
    CIFixStage().run(t, ctx)
    CIFixStage().run(t, ctx)
    assert _read_counter(cycle_path) == 2

    # CI turns green → re-poll, but the cycle counter is NOT reset here. A
    # flickering CI emits a transient "success" between failing cycles;
    # resetting on that let a runaway loop survive ~200 cycles. The counter is
    # reset only on genuine forward progress (merge → HUMAN_MR_APPROVAL).
    state["conclusion"] = "success"
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert _read_counter(cycle_path) == 2  # persists across the transient green


def test_max_cycles_zero_disables_ceiling(tmp_path, monkeypatch):
    """When ci_fix_max_cycles=0, the hard ceiling never fires (loop relies
    solely on the existing caps)."""
    ctx = _gh(tmp_path, ci_fix_max_cycles="0")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    agent_calls = {"n": 0}

    def fake_agent(**k):
        agent_calls["n"] += 1
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        fake_agent,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "local-sha",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.remote_branch_sha",
        lambda repo, branch: "remote-sha",
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    # Run 10 cycles — none should block on the hard ceiling.
    for _ in range(10):
        out = CIFixStage().run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE
    assert agent_calls["n"] == 10


def test_cycle_counter_not_incremented_on_transient_repoll(tmp_path, monkeypatch):
    """A pending/None conclusion does not run the agent and must not bump
    the cycle counter."""
    ctx = _gh(tmp_path, ci_fix_max_cycles="3")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert _read_counter(cycle_path) == 0


# --- Fix success + push failure → BLOCKED ---


def test_fix_success_push_failure_blocks(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda **k: (_ for _ in ()).throw(RuntimeError("remote rejected")),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "force-push failed" in out.note


# --- Fix failure, attempts remaining → IMPLEMENT_COMPLETE ---


def test_fix_failure_retries_next_poll(tmp_path, monkeypatch):
    ctx = _gh(tmp_path, ci_fix_max_attempts="3")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "test", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="FAILED", summary="nope"),
    )

    push_calls = []

    def fake_push(*a, **k):
        push_calls.append(1)

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.git_ops.push", fake_push)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_attempts.txt"

    # Attempt 1: fails → IMPLEMENT_COMPLETE, counter=1
    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.IMPLEMENT_COMPLETE
    assert _read_counter(counter) == 1
    assert push_calls == []  # never pushed on failure


# --- Fix failure, attempts exhausted → BLOCKED ---


def test_fix_failure_exhausted_blocks(tmp_path, monkeypatch):
    ctx = _gh(tmp_path, ci_fix_max_attempts="2")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "test", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="FAILED", summary="nope"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_attempts.txt"

    # Attempt 1: fails → IMPLEMENT_COMPLETE
    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.IMPLEMENT_COMPLETE

    # Attempt 2: fails → BLOCKED (exhausted)
    out2 = CIFixStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert "ci fix failed after 2 attempt" in out2.note

    # Counter reset on exhaustion.
    assert _read_counter(counter) == 0


# --- Agent crash → treated as failure ---


def test_agent_crash_treated_as_failure(tmp_path, monkeypatch):
    ctx = _gh(tmp_path, ci_fix_max_attempts="1")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: (_ for _ in ()).throw(RuntimeError("LLM timeout")),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "ci fix failed after 1 attempt" in out.note


# --- Missing workspace clone → BLOCKED ---


def test_missing_workspace_clone_blocks(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    # No repo dir created.

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "workspace clone is missing" in out.note


# --- Forge not configured → BLOCKED ---


def test_forge_not_configured_blocks(tmp_path):
    ctx = _ctx(tmp_path)
    out = CIFixStage().run(_fixing_ci(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "forge not configured" in out.note


def test_auto_forge_kind_bypasses_none_guard(tmp_path):
    """forge_kind=auto with a valid remote_url bypasses the
    forge_kind=none guard and does not block with 'forge not configured'."""
    ctx = _ctx(
        tmp_path,
        FORGE_KIND="auto",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
    )
    out = CIFixStage().run(_fixing_ci(ctx), ctx)
    # Should NOT block due to forge_kind=none. May fail for other
    # reasons (e.g. no workspace clone), but the note must not contain
    # the "forge not configured" sentinel.
    assert "forge not configured" not in out.note


# --- Force-push refspec is ticket branch only ---


def test_force_push_refspec_is_ticket_branch_only(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    push_args = {}

    def fake_push(repo, branch, remote_url, token):
        push_args.update(branch=branch, remote_url=remote_url, token=token)

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.git_ops.push", fake_push)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)
    assert push_args["branch"] == f"mill/{t.id}"
    assert push_args["branch"] != "main"


# --- CI green/pending while in FIXING_CI → back to IMPLEMENT_COMPLETE ---


def test_ci_green_while_in_fixing_ci_returns_implement_complete(tmp_path, monkeypatch):
    """If CI turns green while we're in FIXING_CI, go back to IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_ci_pending_while_in_fixing_ci_returns_implement_complete(
    tmp_path, monkeypatch
):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_check_status_returns_none_while_in_fixing_ci(tmp_path, monkeypatch):
    """PR disappeared → back to IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_check_status_exception_while_in_fixing_ci(tmp_path, monkeypatch):
    """Transient error → back to IMPLEMENT_COMPLETE for re-poll."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("api down")),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


# --- Counter location ---


def test_counter_location_is_artifacts_dir(tmp_path, monkeypatch):
    """Counter is at artifacts_dir / ci_fix_attempts.txt."""
    ctx = _gh(tmp_path, ci_fix_max_attempts="3")
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "test", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="FAILED", summary="nope"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)

    counter_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_attempts.txt"
    assert counter_path.exists()
    assert _read_counter(counter_path) == 1


# --- _build_failing_summary ---


def test_build_failing_summary_formats_correctly():
    failing = [
        {
            "name": "lint / ruff",
            "summary": "Found 3 errors",
            "text": "line 1: unused import\nline 2: missing docstring",
            "annotations": [
                {
                    "path": "src/foo.py",
                    "start_line": 10,
                    "message": "unused import os",
                    "level": "failure",
                },
            ],
        },
        {
            "name": "test / pytest",
            "summary": None,
            "text": None,
            "annotations": [],
        },
    ]
    result = _build_failing_summary(failing)
    assert "## Failing check #1: lint / ruff" in result
    assert "Found 3 errors" in result
    assert "unused import" in result
    assert "src/foo.py:10" in result
    assert "## Failing check #2: test / pytest" in result


def test_build_failing_summary_empty():
    assert _build_failing_summary([]) == ""


# --- Counter helpers ---


def test_ci_fix_counter_read_write(tmp_path):
    p = tmp_path / "ci_fix_counter.txt"
    assert _read_counter(p) == 0
    p.write_text("garbage")
    assert _read_counter(p) == 0
    _write_counter(p, 5)
    assert _read_counter(p) == 5
    _write_counter(p, 0)
    assert _read_counter(p) == 0


# ---------------------------------------------------------------------------
# _build_failing_summary with log_text
# ---------------------------------------------------------------------------


def test_build_failing_summary_includes_job_logs():
    """_build_failing_summary includes **Job logs:** section when log_text provided."""
    failing = [
        {"name": "docker-build", "summary": None, "text": None, "annotations": []},
    ]
    result = _build_failing_summary(failing, log_text="ERROR: build failed\n")
    assert "**Job logs:**" in result
    assert "ERROR: build failed" in result


def test_build_failing_summary_no_logs_still_works():
    """Existing path unchanged when log_text is None/empty."""
    failing = [
        {"name": "lint", "summary": "err", "text": None, "annotations": []},
    ]
    result = _build_failing_summary(failing)
    assert "**Job logs:**" not in result
    assert "## Failing check #1: lint" in result


def test_ci_fix_stage_fetches_job_logs_on_failure(tmp_path, monkeypatch):
    """Mock list_workflow_runs + fetch_workflow_job_logs; verify
    _build_failing_summary receives the log text."""
    ctx = _gh(tmp_path)
    # PR status returns a sha.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "http://pr",
            "mergeable": True,
            "sha": "abc123",
        },
    )
    # check_status returns failure.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "build", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    # list_workflow_runs returns one failed run.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: [
            {
                "id": 42,
                "name": "CI",
                "workflow_id": 100,
                "head_sha": "abc123",
                "conclusion": "failure",
                "html_url": "http://x",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ],
    )
    # fetch_workflow_job_logs returns log text.
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, *, run_id: "docker build error\n",
    )
    # ci-fix agent succeeds.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    # push succeeds.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_build_failing_summary_includes_codeql_alerts():
    from robotsix_mill.stages.ci_fix import _build_failing_summary

    out = _build_failing_summary(
        failing=[{"name": "CodeQL"}],
        log_text="",
        alerts=[
            {
                "rule": "py/x",
                "severity": "high",
                "path": "t.py",
                "line": 9,
                "message": "bad",
            }
        ],
    )
    assert "Code-scanning alerts" in out
    assert "py/x" in out and "t.py:9" in out and "high" in out


# ---------------------------------------------------------------------------
# OUT_OF_SCOPE → spawn fix ticket + park + auto-resume
# ---------------------------------------------------------------------------


def _oos_forge(
    monkeypatch,
    *,
    alert_paths=("src/pkg/__init__.py",),
    pr_paths=("src/other.py",),
):
    """Wire the forge seams for an OUT_OF_SCOPE run (failing CI + a sha).

    Also wires the code-scanning + pr_files seams the deterministic in-diff
    guard consumes. By default the alert path is NOT among the PR's changed
    files (all-untouched), so the guard falls through to the spawn path.
    """
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "alert", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [
            {
                "rule": "py/clear-text-logging",
                "severity": "high",
                "path": p,
                "line": 3,
                "message": "alert",
            }
            for p in alert_paths
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [
            {"path": p, "status": "modified", "additions": 1, "deletions": 0}
            for p in pr_paths
        ],
    )


def _oos_result(**over):
    kwargs = dict(
        status="OUT_OF_SCOPE",
        summary="repo debt — not this ticket's diff",
        out_of_scope_reason="alert lives in __init__.py, outside this ticket's diff",
        failing_check="py/clear-text-logging",
        required_change_area="src/pkg/__init__.py",
    )
    kwargs.update(over)
    return CiFixResult(**kwargs)


def test_partition_alerts_by_diff_splits_in_and_out_of_scope():
    """In-diff alerts land in in_scope; untouched and empty-path alerts land
    in out_of_scope (AC2)."""
    in_diff = {"rule": "py/x", "path": "src/a.py", "line": 1}
    untouched = {"rule": "py/y", "path": "src/b.py", "line": 2}
    no_path = {"rule": "py/z", "path": "", "line": 3}
    missing_path = {"rule": "py/w", "line": 4}
    changed = {"src/a.py", "src/c.py"}

    in_scope, out_of_scope = _partition_alerts_by_diff(
        [in_diff, untouched, no_path, missing_path], changed
    )
    assert in_scope == [in_diff]
    assert out_of_scope == [untouched, no_path, missing_path]


def test_build_failing_summary_labels_in_diff_alert():
    """When changed_paths is provided, in-diff alerts are labelled 'must fix'
    with the rule id + path:line and the explicit in-scope directive (AC3)."""
    out = _build_failing_summary(
        failing=[{"name": "CodeQL"}],
        log_text="",
        alerts=[
            {
                "rule": "py/unused-global-variable",
                "severity": "warning",
                "path": "src/pkg/mod.py",
                "line": 12,
                "message": "unused",
            }
        ],
        changed_paths={"src/pkg/mod.py"},
    )
    assert "py/unused-global-variable" in out
    assert "src/pkg/mod.py:12" in out
    assert (
        "are located in THIS PR's own changed files and MUST be fixed in-scope" in out
    )
    assert "IN THIS PR'S DIFF — must fix" in out


def test_all_in_diff_alerts_suppress_dependency_fixer(tmp_path, monkeypatch):
    """All alerts inside the PR's own diff → no dependency fixer spawned, route
    back to IMPLEMENT_COMPLETE for an in-scope re-run, no force-push (AC1)."""
    ctx = _gh(tmp_path)
    _oos_forge(
        monkeypatch,
        alert_paths=("src/pkg/mod.py",),
        pr_paths=("src/pkg/mod.py",),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    push_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: push_calls.append(1),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY) == []
    assert push_calls == []


def test_out_of_scope_description_names_untouched_alert(tmp_path, monkeypatch):
    """The spawned out-of-scope ticket's description names the untouched
    alert's rule id + path (AC3)."""
    ctx = _gh(tmp_path)
    _oos_forge(
        monkeypatch,
        alert_paths=("src/untouched.py",),
        pr_paths=("src/other.py",),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    fix = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)[0]
    desc = ctx.service.workspace(fix).read_description()
    assert "py/clear-text-logging" in desc
    assert "src/untouched.py" in desc


def test_out_of_scope_spawns_fix_ticket_and_parks(tmp_path, monkeypatch):
    """An OUT_OF_SCOPE verdict creates exactly one fix ticket, wires
    depends_on/unblocks both ways, parks the original to BLOCKED, and never
    pushes."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    push_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: push_calls.append(1),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "out of scope" in out.note
    # The OUT_OF_SCOPE path never force-pushes.
    assert push_calls == []

    # Exactly one fix ticket on the same board.
    fixes = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)
    assert len(fixes) == 1
    fix = fixes[0]
    assert fix.board_id == "test-board"
    assert fix.source == SourceKind.CI_FIX_DEPENDENCY

    # Dependency wired both directions.
    orig = ctx.service.get(t.id)
    assert json.loads(orig.depends_on) == [fix.id]
    assert json.loads(fix.unblocks) == [t.id]


def test_out_of_scope_is_idempotent_across_cycles(tmp_path, monkeypatch):
    """A second OUT_OF_SCOPE cycle with the same failing_check +
    required_change_area (while the fix ticket is still open) reuses the
    existing ticket instead of creating a duplicate."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.BLOCKED
    out2 = CIFixStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED

    fixes = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)
    assert len(fixes) == 1


def test_out_of_scope_fix_done_auto_resumes_original(tmp_path, monkeypatch):
    """When the spawned fix ticket reaches DONE, the existing _fire_unblocks
    path moves the parked original BLOCKED → DRAFT."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    # Simulate the worker applying the stage outcome (FIXING_CI → BLOCKED).
    ctx.service.transition(t.id, State.BLOCKED, note=out.note)

    fix = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)[0]

    # Fix ticket completes → original is auto-unblocked to DRAFT.
    ctx.service.transition(fix.id, State.DONE)
    orig = ctx.service.get(t.id)
    assert orig.state is State.DRAFT


def test_in_scope_done_still_pushes_no_fix_ticket(tmp_path, monkeypatch):
    """Regression: an in-scope DONE verdict still force-pushes and returns
    IMPLEMENT_COMPLETE without spawning any out-of-scope fix ticket."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="fixed"),
    )
    push_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: push_calls.append(1),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert push_calls == [1]
    assert ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY) == []


# ---------------------------------------------------------------------------
# OUT_OF_SCOPE on a stale branch → refresh instead of spawn
# ---------------------------------------------------------------------------


def test_out_of_scope_stale_branch_refreshes_no_spawn(tmp_path, monkeypatch):
    """A branch reporting mergeable_state == 'behind' is refreshed once via
    forge.update_branch instead of spawning a dependency fix."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "alert", "text": None, "annotations": []}
            ],
        },
    )
    # pr_status reports the branch is behind its base.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "sha": "abc123",
            "mergeable": True,
            "mergeable_state": "behind",
        },
    )
    update_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: (
            update_calls.append(source_branch)
            or {"updated": True, "reason": "update-branch accepted"}
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    push_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: push_calls.append(1),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert update_calls == [f"mill/{t.id}"]
    # No dependency fix spawned and the parent's depends_on is unchanged.
    assert ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY) == []
    orig = ctx.service.get(t.id)
    assert not orig.depends_on or json.loads(orig.depends_on) == []
    assert push_calls == []
    # Refresh counter recorded.
    refresh_path = (
        ctx.service.workspace(t).artifacts_dir / "ci_fix_refresh_attempts.txt"
    )
    assert _read_counter(refresh_path) == 1


def test_out_of_scope_clean_branch_spawns_fix(tmp_path, monkeypatch):
    """A branch reporting mergeable_state == 'clean' (up to date) spawns the
    dependency fix exactly as before — update_branch is never called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "alert", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "sha": "abc123",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )
    update_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: (
            update_calls.append(source_branch) or {"updated": True}
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert update_calls == []
    fixes = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)
    assert len(fixes) == 1


def test_out_of_scope_stale_branch_refresh_capped_at_one(tmp_path, monkeypatch):
    """When the refresh counter is already >= 1, a still-behind branch does
    NOT re-call update_branch and falls through to the normal spawn path."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "alert", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "sha": "abc123",
            "mergeable": True,
            "mergeable_state": "behind",
        },
    )
    update_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: (
            update_calls.append(source_branch) or {"updated": True}
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        lambda *a, **k: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    # Pre-seed the refresh counter so a prior refresh already happened.
    refresh_path = (
        ctx.service.workspace(t).artifacts_dir / "ci_fix_refresh_attempts.txt"
    )
    _write_counter(refresh_path, 1)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    # No second update_branch call.
    assert update_calls == []
    fixes = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)
    assert len(fixes) == 1


# ---------------------------------------------------------------------------
# GitHubForge.update_branch HTTP mapping
# ---------------------------------------------------------------------------


def test_github_update_branch_http_mapping(tmp_path, monkeypatch):
    """update_branch maps HTTP 202 → updated, 422 → already up to date,
    other → failure, and missing PR → not found."""
    ctx = _gh(tmp_path)
    forge = github.GitHubForge(ctx.settings, repo_config=ctx.repo_config)

    monkeypatch.setattr(
        github.GitHubForge,
        "_get_pr",
        lambda self, *, owner, repo, head: {"number": 7},
    )

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    put_calls = []

    def fake_put(path, **kw):
        put_calls.append(path)
        return _Resp(status_map["code"], status_map.get("text", ""))

    monkeypatch.setattr(forge._http, "put", fake_put)

    status_map = {"code": 202}
    assert forge.update_branch(source_branch="b")["updated"] is True
    assert put_calls[-1] == "/repos/o/r/pulls/7/update-branch"

    status_map = {"code": 422}
    res = forge.update_branch(source_branch="b")
    assert res["updated"] is False
    assert res["reason"] == "already up to date"

    status_map = {"code": 500, "text": "boom"}
    res = forge.update_branch(source_branch="b")
    assert res["updated"] is False
    assert "HTTP 500" in res["reason"]

    # Missing PR.
    monkeypatch.setattr(
        github.GitHubForge,
        "_get_pr",
        lambda self, *, owner, repo, head: None,
    )
    res = forge.update_branch(source_branch="b")
    assert res == {"updated": False, "reason": "PR not found"}
