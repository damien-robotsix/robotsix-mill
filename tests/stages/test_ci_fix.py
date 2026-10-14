"""Tests for the CIFixStage (FIXING_CI → IMPLEMENT_COMPLETE | BLOCKED)."""

import subprocess

import pytest

from robotsix_mill.agents.ci_fixing import CiFixResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.ci_fix import (
    _CI_PUSH_RETRY_BUDGET,
    CIFixStage,
)
from robotsix_mill.stages.ci_fix_helpers import (
    _build_failing_summary,
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


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.pop("FORGE_TOKEN", None)
    if ft is not None:
        import robotsix_mill.config as _cfg
        from robotsix_mill.config import Secrets, _reset_secrets

        _reset_secrets()
        _cfg._secrets = Secrets(forge_token=ft)
    s = Settings(**env)
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
        forge_kind="github",
        FORGE_TOKEN="t",
        forge_remote_url="https://github.com/o/r.git",
        **extra,
    )


def _setup_repo(ctx, ticket):
    """Create a minimal .git in the workspace so _workspace_repo_dir succeeds."""
    repo_dir = ctx.service.workspace(ticket).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)
    return str(repo_dir)


def _failing_check_status(monkeypatch):
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
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "CodeQL", "summary": "alert", "text": None, "annotations": []}
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
        lambda self, *, source_branch, require_checks=False: [
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
        lambda self, *, source_branch, require_checks=False: [
            {"path": p, "status": "modified", "additions": 1, "deletions": 0}
            for p in pr_paths
        ],
    )


def _oos_result(**over):
    kwargs = {
        "status": "OUT_OF_SCOPE",
        "summary": "repo debt — not this ticket's diff",
        "out_of_scope_reason": "alert lives in __init__.py, outside this ticket's diff",
        "failing_check": "py/clear-text-logging",
        "required_change_area": "src/pkg/__init__.py",
    }
    kwargs.update(over)
    return CiFixResult(**kwargs)


# --- Fix success + push success → IMPLEMENT_COMPLETE ---


def test_fix_success_push_success_returns_implement_complete(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
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
    # pr_status is called to get head_sha for job-log fetching.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, target=target, token=token)
        return git_ops.PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        fake_post_check,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert post_check_calls["branch"] == f"mill/{t.id}"

    # Counter reset to 0.
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_attempts.txt"
    assert _read_counter(counter) == 0


# --- Memory ledger read is capped at max_memory_chars ---


def test_ci_fix_memory_read_is_tail_truncated(tmp_path, monkeypatch):
    """When the on-disk ci_fix_memory.md exceeds max_memory_chars, the memory
    string handed to the ci-fix agent is tail-truncated and begins with the
    ``[... memory truncated: N chars omitted]`` marker."""
    ctx = _gh(tmp_path, max_memory_chars="100")
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
    seen = {}

    def fake_agent(**k):
        seen["memory"] = k["memory"]
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

    # Seed a ledger larger than max_memory_chars (multi-line so tail_keep can
    # advance to a newline boundary).
    mem_path = ctx.settings.memory_file_for("ci_fix", ctx.memory_board_id(t))
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    big = "".join(f"line {i} of the ci_fix memory ledger\n" for i in range(50))
    mem_path.write_text(big, encoding="utf-8")
    assert len(big) > 100

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert seen["memory"].startswith("[... memory truncated:")
    # The kept tail (everything after the marker) is bounded by the cap.
    assert big[-100:].splitlines()[-1] in seen["memory"]


def test_ci_fix_memory_read_passthrough_when_small(tmp_path, monkeypatch):
    """When the ledger is smaller than max_memory_chars, the content is passed
    through unchanged (no truncation marker)."""
    ctx = _gh(tmp_path, max_memory_chars="8000")
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
    seen = {}

    def fake_agent(**k):
        seen["memory"] = k["memory"]
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

    mem_path = ctx.settings.memory_file_for("ci_fix", ctx.memory_board_id(t))
    mem_path.parent.mkdir(parents=True, exist_ok=True)
    small = "a short ci_fix ledger\n"
    mem_path.write_text(small, encoding="utf-8")

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert seen["memory"] == small
    assert "memory truncated:" not in seen["memory"]


def test_fix_success_push_failure_blocks(tmp_path, monkeypatch):
    """Agent DONE but the push never lands even after the bounded auto-retry
    budget (every re-push is lease-rejected) — BLOCKED with a transient
    lease-rejection note, not a misleading 'could not fix' note."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: (
            git_ops.PostPushResult.NOT_LANDED
        ),
    )
    # The rebase always succeeds; every re-push is rejected by the lease —
    # the transient case the auto-retry is designed to recover from.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto_branch",
        lambda *a, **k: True,
    )
    push_attempts = {}

    def fake_push(repo, branch, remote_url, token):
        push_attempts["n"] = push_attempts.get("n", 0) + 1
        raise subprocess.CalledProcessError(1, ["git", "push"])

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease", fake_push
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "push did not land" in out.note
    # The block note distinguishes a transient push lease rejection from a
    # substantive 'agent could not produce a fix'.
    assert "push lease rejection" in out.note
    assert "transient" in out.note
    # The auto-retry is bounded, not an unbounded loop.
    assert push_attempts["n"] == _CI_PUSH_RETRY_BUDGET


def test_fix_success_push_not_landed_retry_relands(tmp_path, monkeypatch):
    """Agent DONE, the first post-check is NOT_LANDED, but the auto-retry
    (rebase onto the current remote tip + re-push with lease) lands the fix —
    returns IMPLEMENT_COMPLETE instead of blocking."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    outcomes = iter(
        [
            git_ops.PostPushResult.NOT_LANDED,
            git_ops.PostPushResult.PASS,
        ]
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: next(outcomes),
    )
    rebased = {}

    def fake_rebase(repo, branch, *, remote_url, token):
        rebased["n"] = rebased.get("n", 0) + 1
        return True

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto_branch", fake_rebase
    )
    pushed = {}

    def fake_push(repo, branch, remote_url, token):
        pushed["n"] = pushed.get("n", 0) + 1

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease", fake_push
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # Rebased once and re-pushed once before the fix landed.
    assert rebased["n"] == 1
    assert pushed["n"] == 1


def test_fix_success_push_rebase_conflict_blocks(tmp_path, monkeypatch):
    """Agent DONE, the push did not land, and the fix cannot be re-applied
    onto the current remote tip (rebase conflict) — blocks immediately as a
    substantive fix failure rather than retrying pointlessly."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: (
            git_ops.PostPushResult.NOT_LANDED
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto_branch",
        lambda *a, **k: False,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "push did not land" in out.note
    assert "could not be re-applied" in out.note
    assert "rebase conflict" in out.note


def test_fix_success_push_retry_foreign_divergence_blocks(tmp_path, monkeypatch):
    """Agent DONE, push did not land, auto-retry lands it, but the post-check
    then finds foreign-authored commits on the branch — blocks for manual
    reconciliation (a human pushed), never force-pushing over them."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    outcomes = iter(
        [
            git_ops.PostPushResult.NOT_LANDED,
            git_ops.PostPushResult.FOREIGN_DIVERGENCE,
        ]
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: next(outcomes),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "foreign-authored commits" in out.note
    assert "Manual reconciliation" in out.note


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
        forge_kind="auto",
        FORGE_TOKEN="t",
        forge_remote_url="https://github.com/o/r.git",
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
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    post_check_args = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_args.update(
            branch=branch, target=target, remote_url=remote_url, token=token
        )
        return git_ops.PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check", fake_post_check
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)
    assert post_check_args["branch"] == f"mill/{t.id}"
    assert post_check_args["branch"] != "main"


# --- CI green/pending while in FIXING_CI → back to IMPLEMENT_COMPLETE ---


def test_ci_green_while_in_fixing_ci_returns_implement_complete(tmp_path, monkeypatch):
    """If CI turns green while we're in FIXING_CI, go back to IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
        },
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
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "pending",
            "failing": [],
        },
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
        lambda self, *, source_branch, require_checks=False: None,
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
        lambda self, *, source_branch, require_checks=False: (_ for _ in ()).throw(
            RuntimeError("api down")
        ),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


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
    assert "## ❌ FAILED: lint / ruff" in result
    assert "Found 3 errors" in result
    assert "unused import" in result
    assert "src/foo.py:10" in result
    assert "## ❌ FAILED: test / pytest" in result


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


# --- Diverged remote PR branch → BLOCKED, never force-push (data-loss guard) ---


def test_reconcile_diverged_blocks_without_pushing(tmp_path, monkeypatch):
    """When reconcile_with_remote_pr returns False (the workspace clone and the
    remote PR branch have diverged — e.g. a human pushed to the PR), the stage
    must BLOCK and must NOT call push_with_lease. push_with_lease cannot protect
    this case: reconcile's own fetch already advanced the lease ref to the
    foreign commit, so a lease push would silently overwrite it."""
    ctx = _gh(tmp_path)
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
    # Diverged: reconcile reports it cannot fast-forward.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.reconcile_with_remote_pr",
        lambda repo, remote_url, branch, token: git_ops.ReconcileResult.DIVERGED,
    )
    pushed = {"called": False}
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: pushed.update(called=True),
    )
    # The agent must never run on a diverged branch.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: (_ for _ in ()).throw(
            AssertionError("agent ran despite diverged branch")
        ),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert pushed["called"] is False
    assert "diverged" in (out.note or "").lower()


def _flip_check_status_to_success_after_first(monkeypatch):
    """check_status returns 'failure' on the first call (stage entry) and
    'success' on every subsequent call (the pre-block CI re-check)."""
    calls = {"n": 0}

    def _status(self, *, source_branch, require_checks=False):
        calls["n"] += 1
        conclusion = "failure" if calls["n"] == 1 else "success"
        return {
            "conclusion": conclusion,
            "failing": (
                [{"name": "lint", "summary": "err", "text": None, "annotations": []}]
                if conclusion == "failure"
                else []
            ),
        }

    monkeypatch.setattr(github.GitHubForge, "check_status", _status)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    return calls


# ---------------------------------------------------------------------------
# Artifact + history note observability
# ---------------------------------------------------------------------------


def test_failing_summary_txt_written_on_failure(tmp_path, monkeypatch):
    """failing_summary.txt is written (non-empty) when CI is detected failing."""
    ctx = _gh(tmp_path)
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
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="fixed lint"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)

    artifacts = ctx.service.workspace(t).artifacts_dir
    summary_path = artifacts / "failing_summary.txt"
    assert summary_path.exists(), "failing_summary.txt must exist after failure"
    content = summary_path.read_text(encoding="utf-8")
    assert content.strip(), "failing_summary.txt must not be empty"
    assert "lint" in content


def test_failing_summary_txt_fallback_when_summary_empty(tmp_path, monkeypatch):
    """When _build_failing_summary produces an empty string, the file still
    contains a fallback with the failing check names."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {"name": "build", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "abc123"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)

    artifacts = ctx.service.workspace(t).artifacts_dir
    summary_path = artifacts / "failing_summary.txt"
    assert summary_path.exists()
    content = summary_path.read_text(encoding="utf-8")
    assert content.strip(), "must not be empty even when summary is empty"
    assert "build" in content


def test_ci_fix_md_written_with_failure_and_agent_recap(tmp_path, monkeypatch):
    """ci_fix.md is written after the agent runs and contains both the
    detected failure and the agent's recap."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "lint",
                    "summary": "ruff found errors",
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
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="applied ruff fixes"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)

    artifacts = ctx.service.workspace(t).artifacts_dir
    md_path = artifacts / "ci_fix.md"
    assert md_path.exists(), "ci_fix.md must exist after a failure-driven cycle"
    content = md_path.read_text(encoding="utf-8")
    assert "Detected Failure" in content
    assert "ruff found errors" in content
    assert "Agent Recap" in content
    assert "**Verdict:** DONE" in content
    assert "applied ruff fixes" in content

    # The history file must also be written.
    history_path = artifacts / "ci_fix_history.md"
    assert history_path.exists(), "ci_fix_history.md must exist"
    history = history_path.read_text(encoding="utf-8")
    assert "Attempt" in history
    assert "ruff found errors" in history
    assert "DONE" in history


def test_ci_fix_md_written_when_agent_crashes(tmp_path, monkeypatch):
    """ci_fix.md is still written when the agent crashes (result is None)."""
    ctx = _gh(tmp_path)
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
    # Simulate agent crash.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)

    artifacts = ctx.service.workspace(t).artifacts_dir
    md_path = artifacts / "ci_fix.md"
    assert md_path.exists(), "ci_fix.md must exist even on agent crash"
    content = md_path.read_text(encoding="utf-8")
    assert "Detected Failure" in content
    assert "Agent Recap" in content
    assert "crashed" in content.lower()

    # History file must also be written on crash.
    history_path = artifacts / "ci_fix_history.md"
    assert history_path.exists(), "ci_fix_history.md must exist even on crash"
    history = history_path.read_text(encoding="utf-8")
    assert "CRASH" in history
    # Directive: the crash type + message must be surfaced so the next
    # attempt sees why the prior one died.
    assert "RuntimeError" in history
    assert "boom" in history


def test_failure_cycle_writes_history_note(tmp_path, monkeypatch):
    """A failure-driven ci-fix cycle records exactly one informative history note."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "lint",
                    "summary": "ruff found errors",
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
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="applied ruff fixes"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    # Count history notes before the cycle.
    notes_before = len(ctx.service.history(t.id))

    CIFixStage().run(t, ctx)

    notes_after = len(ctx.service.history(t.id))
    # Expect exactly one new history note from the ci-fix cycle.
    assert notes_after == notes_before + 1, (
        f"expected 1 new note, got {notes_after - notes_before}"
    )

    events = ctx.service.history(t.id)
    last_note = events[-1]
    assert "CI Fix Cycle" in last_note.note
    assert "Detected Failure" in last_note.note
    assert "ruff found errors" in last_note.note
    assert "Agent Result" in last_note.note
    assert "**Verdict:** DONE" in last_note.note
    assert "applied ruff fixes" in last_note.note


def test_history_note_omits_job_logs(tmp_path, monkeypatch):
    """The per-cycle history note must NOT embed the raw job-log window.

    Regression (2026-09-06): a ticket with 9 ci-fix cycles carried ~17KB of
    raw runner logs per note, serving a >100KB /history response that blew
    the token budget of every agent reading it. Logs stay in the agent
    prompt and the ci_fix.md artifact; the note keeps the structured part.
    """
    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    log_body = "2026-09-05T15:25:48.7115316Z Current runner version: '2.337.0'\n" * 50
    failing_summary = (
        "## ❌ Tests\n\n**Summary:**\npytest failed\n\n"
        "**Job logs:**\n```\n" + log_body + "\n```\n"
    )

    CIFixStage()._add_ci_fix_history_note(
        ctx, t, failing_summary, CiFixResult(status="DONE", summary="fixed")
    )

    last_note = ctx.service.history(t.id)[-1]
    assert "pytest failed" in last_note.note
    assert "Current runner version" not in last_note.note
    assert "job logs omitted" in last_note.note
    assert len(last_note.note) < 2000


def test_ci_fix_history_appends_across_attempts(tmp_path, monkeypatch):
    """ci_fix_history.md accumulates entries across multiple ci_fix runs."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "lint",
                    "summary": "ruff found errors",
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
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    call_count = 0

    def _agent(**k):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return CiFixResult(status="FAILED", summary="tried X, did not work")
        return CiFixResult(status="DONE", summary="fixed with Y")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", _agent)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    # First run — FAILED.
    CIFixStage().run(t, ctx)

    artifacts = ctx.service.workspace(t).artifacts_dir
    history_path = artifacts / "ci_fix_history.md"
    assert history_path.exists()
    history1 = history_path.read_text(encoding="utf-8")
    assert "FAILED" in history1
    assert "tried X" in history1

    # Second run — DONE.
    CIFixStage().run(t, ctx)
    history2 = history_path.read_text(encoding="utf-8")
    # Must contain BOTH attempts.
    assert history2.count("## Attempt") == 2
    assert "DONE" in history2
    assert "fixed with Y" in history2


def test_ci_fix_history_trims_to_max_entries(tmp_path, monkeypatch):
    """ci_fix_history.md is trimmed to the most recent 20 attempts."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "lint",
                    "summary": "ruff found errors",
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
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="fixed with Z"),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    # Pre-seed a history file with more than 20 attempts.
    artifacts = ctx.service.workspace(t).artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    seeded = ["# CI Fix Attempt History\n\n"]
    for i in range(25):
        seeded.append(f"## Attempt\n**Failure:** seed {i}\n**Verdict:** FAILED\n\n")
    (artifacts / "ci_fix_history.md").write_text("".join(seeded), encoding="utf-8")

    CIFixStage().run(t, ctx)

    history_path = artifacts / "ci_fix_history.md"
    history = history_path.read_text(encoding="utf-8")
    # 25 seeded + 1 new attempt trimmed back to 20.
    assert history.count("## Attempt") == 20
    # The newest entry survives; the oldest seeded ones are dropped.
    assert "fixed with Z" in history
    assert "seed 0" not in history
    assert "seed 5" not in history
    assert "seed 6" in history


def test_ci_fix_previous_attempts_passed_to_agent(tmp_path, monkeypatch):
    """The previous_attempts kwarg is populated from ci_fix_history.md."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "lint",
                    "summary": "ruff found errors",
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
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    # Pre-seed a history file with a prior attempt.
    artifacts = ctx.service.workspace(t).artifacts_dir
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "ci_fix_history.md").write_text(
        "# CI Fix Attempt History\n\n## Attempt\n**Failure:** old failure\n"
        "**Verdict:** FAILED\ntried approach A\n\n",
        encoding="utf-8",
    )

    captured_kwargs: dict = {}

    def _agent(**k):
        captured_kwargs.update(k)
        return CiFixResult(status="DONE", summary="fixed")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", _agent)

    CIFixStage().run(t, ctx)

    assert "previous_attempts" in captured_kwargs
    assert "old failure" in captured_kwargs["previous_attempts"]
    assert "tried approach A" in captured_kwargs["previous_attempts"]


def test_success_repoll_does_not_write_history_note(tmp_path, monkeypatch):
    """A benign re-poll path (conclusion=success) records NO history note."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
        },
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    notes_before = len(ctx.service.history(t.id))
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    notes_after = len(ctx.service.history(t.id))
    assert notes_after == notes_before, (
        f"success re-poll must not add a note, but {notes_after - notes_before} added"
    )


def test_pending_repoll_does_not_write_history_note(tmp_path, monkeypatch):
    """A benign re-poll path (conclusion=pending) records NO history note."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "pending",
            "failing": [],
        },
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    notes_before = len(ctx.service.history(t.id))
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    notes_after = len(ctx.service.history(t.id))
    assert notes_after == notes_before, (
        f"pending re-poll must not add a note, but {notes_after - notes_before} added"
    )


def test_check_status_none_does_not_write_history_note(tmp_path, monkeypatch):
    """PR-disappeared re-poll (status is None) records NO history note."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    notes_before = len(ctx.service.history(t.id))
    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    notes_after = len(ctx.service.history(t.id))
    assert notes_after == notes_before, (
        f"status-None re-poll must not add a note, but {notes_after - notes_before} added"
    )


# ---------------------------------------------------------------------------
# Regression: ci_fix push uses the same github_token() → _authed_url() path
# as rebase — not a raw FORGE_TOKEN or a sandbox credential.
# ---------------------------------------------------------------------------


def test_ci_fix_agent_push_uses_minted_token_not_raw_forge_token(tmp_path, monkeypatch):
    """The ci-fix agent push (via post_push_check) must use github_push_token()
    — not the raw s.forge_token, which is empty under GitHub App auth.
    Mirrors ``test_rebase_force_push_uses_minted_token_not_raw_forge_token``
    for the rebase stage."""
    ctx = _gh(tmp_path)  # FORGE_TOKEN="t" (raw); minted token differs
    _failing_check_status(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.github_push_token",
        lambda s, repo_config=None: "MINTED-APP-TOK",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.github_token",
        lambda s, repo_config=None: "MINTED-APP-TOK",
    )
    seen = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        seen.update(token=token, remote_url=remote_url)
        return git_ops.PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        fake_post_check,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert seen.get("token") == "MINTED-APP-TOK"  # not the raw "t"
    # The remote URL must also come from _resolve_remote_url, not be empty.
    assert seen.get("remote_url") == "https://github.com/o/r.git"


def test_ci_fix_and_rebase_use_same_token_function(
    tmp_path,
    monkeypatch,
):
    """Both ci_fix and rebase resolve tokens through the SAME
    ``github_token()`` function — there is no separate sandbox or
    pipeline push path."""
    import robotsix_mill.stages.ci_fix as ci_fix_mod
    import robotsix_mill.stages.merge as merge_mod

    # Both modules must import github_token from the same source.
    assert ci_fix_mod.github_token is merge_mod.github_token

    # Verify the shared source is forge.auth.github_token.
    from robotsix_mill.forge.auth import github_token as canonical

    assert ci_fix_mod.github_token is canonical
    assert merge_mod.github_token is canonical


# ---------------------------------------------------------------------------
# Regression: resume-blocked must force a fresh CI run
# ---------------------------------------------------------------------------


def test_stale_branch_reruns_workflow_for_transient_failure(tmp_path, monkeypatch):
    """When a CI failure is transient (e.g. ECONNRESET), the stage re-runs
    the failing workflow(s) via the forge API instead of pushing an empty
    commit — no noise commits, and the identical-failure gate bounds
    repeated re-triggers.
    """
    ctx = _gh(tmp_path)

    # Simulate a branch that is already current: rebase succeeds but
    # produces no diff.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto",
        lambda *a, **k: True,
    )

    push_calls = []

    def track_push(repo, branch, remote_url, token):
        push_calls.append((branch, remote_url))

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        track_push,
    )
    # head_sha and ls_remote_sha — still needed by the rebase path but
    # the empty-commit logic is removed; these just prevent crashes.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "abc123",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.ls_remote_sha",
        lambda remote_url, ref, token=None: "abc123",
    )

    # check_status returns failure with transient signature in the logs.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "tests",
                    "summary": "test suite failed",
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
        "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: [
            {
                "id": 42,
                "name": "tests",
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
        lambda self, *, run_id, full_log=False: "pytest failed: ECONNRESET\n",
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

    rerun_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "rerun_workflow",
        lambda self, *, run_id: rerun_calls.append(run_id) or {"rerun": True},
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    # The transient failure is re-run; ticket re-polls.
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # rerun_workflow was called (not empty_commit).
    assert rerun_calls == [42]
    # Only one push: the rebase push (no empty commit).
    assert len(push_calls) == 1


def test_branch_changed_by_rebase_skips_empty_commit(tmp_path, monkeypatch):
    """When the rebase actually changes HEAD (e.g. main advanced), the
    push already triggers a fresh CI run — no empty commit is needed.
    """
    ctx = _gh(tmp_path)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto",
        lambda *a, **k: True,
    )

    empty_commit_calls = []

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.empty_commit",
        lambda repo, message: empty_commit_calls.append(message),
    )
    # head_sha and ls_remote_sha differ → empty-commit path skipped.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "new_sha",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.ls_remote_sha",
        lambda remote_url, ref, token=None: "old_sha",
    )

    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "new_sha"},
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # No empty commit was created — the rebase already changed HEAD.
    assert len(empty_commit_calls) == 0


def test_remote_sha_unavailable_skips_empty_commit(tmp_path, monkeypatch):
    """When the remote branch SHA cannot be resolved (e.g. PR not yet
    created, token expired), the empty-commit path is skipped safely
    rather than crashing.
    """
    ctx = _gh(tmp_path)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto",
        lambda *a, **k: False,
    )

    empty_commit_calls = []

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.empty_commit",
        lambda repo, message: empty_commit_calls.append(message),
    )
    # head_sha returns a value but ls_remote_sha returns None.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.head_sha",
        lambda repo: "abc123",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.ls_remote_sha",
        lambda remote_url, ref, token=None: None,
    )

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
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    # The stage proceeds to the agent (which returns DONE).
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # No empty commit — ls_remote_sha returned None.
    assert len(empty_commit_calls) == 0


# ---------------------------------------------------------------------------
# Agent timeout (ci_fix_agent_timeout_seconds)
# ---------------------------------------------------------------------------


def test_agent_timeout_zero_runs_directly(tmp_path, monkeypatch):
    """When ci_fix_agent_timeout_seconds=0, the executor is skipped and
    the agent runs directly on the calling thread."""
    ctx = _gh(tmp_path, ci_fix_agent_timeout_seconds="0")
    _failing_check_status(monkeypatch)

    agent_calls = []

    def fake_agent(**k):
        agent_calls.append(1)
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        fake_agent,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    stage = CIFixStage()
    out = stage.run(t, ctx)

    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert agent_calls == [1]
    # No timeout flags should be set.
    assert stage._last_agent_timed_out is False
    assert stage._last_agent_timeout_elapsed == 0.0


def test_agent_timeout_produces_diagnostic_note(tmp_path, monkeypatch):
    """When the agent times out (result=None, _last_agent_timed_out=True),
    _run_agent_and_finalize emits a diagnostic BLOCKED note that names the
    failing check(s) and the elapsed time."""
    ctx = _gh(tmp_path, ci_fix_agent_timeout_seconds="1800")
    _failing_check_status(monkeypatch)

    # Simulate a timeout: _invoke_agent returns None and sets the flags.
    def fake_invoke(self, ticket, ctx, repo_dir, branch, failing_summary):
        self._last_agent_timed_out = True
        self._last_agent_timeout_elapsed = 1850.0

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.CIFixStage._invoke_agent",
        fake_invoke,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    stage = CIFixStage()
    out = stage.run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert out.note is not None
    # The note must name the failing check.
    assert "lint" in out.note
    # The note must include the elapsed time.
    assert "1850s" in out.note
    # The note must be the diagnostic timeout note (not the generic budget one).
    assert "timed out" in out.note
    assert "wall-clock" in out.note


def test_agent_timeout_unknown_check_fallback(tmp_path, monkeypatch):
    """When the failing summary is empty (should not happen but guarded),
    the timeout note falls back to '(unknown)' as the check name."""
    ctx = _gh(tmp_path, ci_fix_agent_timeout_seconds="1800")
    _failing_check_status(monkeypatch)

    # Override check_status to return an empty failing summary.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [],  # empty — produces a nearly-empty summary
        },
    )

    def fake_invoke(self, ticket, ctx, repo_dir, branch, failing_summary):
        self._last_agent_timed_out = True
        self._last_agent_timeout_elapsed = 1200.0

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.CIFixStage._invoke_agent",
        fake_invoke,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    stage = CIFixStage()
    out = stage.run(t, ctx)

    assert out.next_state is State.BLOCKED
    # With no failing checks, _extract_check_names returns "(unknown)".
    assert "(unknown)" in out.note


def test_agent_crash_without_timeout_uses_budget_note(tmp_path, monkeypatch):
    """When _invoke_agent returns None but _last_agent_timed_out is False
    (agent crashed, not timed out), the budget-exhausted note is used
    instead of the timeout diagnostic — and it names the failing check."""
    ctx = _gh(tmp_path, ci_fix_agent_timeout_seconds="1800")
    _failing_check_status(monkeypatch)

    def fake_invoke(self, ticket, ctx, repo_dir, branch, failing_summary):
        # Crash — no timeout flags set.
        return None

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.CIFixStage._invoke_agent",
        fake_invoke,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    stage = CIFixStage()
    out = stage.run(t, ctx)

    assert out.next_state is State.BLOCKED
    # The budget note names the failing check.
    assert "lint" in out.note
    assert "iteration budget" in out.note
    assert "timed out" not in out.note


def test_budget_exhaustion_note_includes_check_and_url(tmp_path, monkeypatch):
    """A ticket blocked by budget exhaustion has a note naming at least one
    failing check and a log URL when run URLs are available."""
    ctx = _gh(tmp_path, ci_fix_agent_timeout_seconds="1800")
    _failing_check_status(monkeypatch)

    # Provide a failing workflow run with a URL so the note includes it.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, head_sha=None, branch=None: [
            {
                "id": 99,
                "conclusion": "failure",
                "name": "CI",
                "html_url": "https://github.com/o/r/actions/runs/99",
            }
        ],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "fetch_workflow_job_logs",
        lambda self, *, run_id, full_log=False: "error log",
    )
    # No code-scanning alerts.
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

    def fake_invoke(self, ticket, ctx, repo_dir, branch, failing_summary):
        return CiFixResult(status="FAILED", summary="could not fix ruff")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.CIFixStage._invoke_agent",
        fake_invoke,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    stage = CIFixStage()
    out = stage.run(t, ctx)

    assert out.next_state is State.BLOCKED
    # Names the failing check.
    assert "lint" in out.note
    # Includes the log URL.
    assert "https://github.com/o/r/actions/runs/99" in out.note
    # Includes the agent's verdict summary.
    assert "could not fix ruff" in out.note
    # Not the generic bare message.
    assert "manual intervention required" in out.note.lower()


# ---------------------------------------------------------------------------
# CI_FAILURE diagnostic event emission
# ---------------------------------------------------------------------------


def test_ci_failure_diagnostic_event_emitted_on_failure(tmp_path, monkeypatch):
    """A CI_FAILURE diagnostic event is emitted every time the ci_fix
    stage confirms CI is genuinely failing.

    Regression test: ensures the recurring-category → auto-fix-proposal
    pipeline receives input and doesn't starve.
    """
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events

    ctx = _gh(tmp_path)
    _failing_check_status(monkeypatch)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    stage = CIFixStage()
    stage.run(t, ctx)

    events = list_diagnostic_events(ctx.settings, "test-board", category="CI_FAILURE")
    assert len(events) == 1, f"expected 1 CI_FAILURE event, got {len(events)}"
    ev = events[0]
    assert ev.ticket_id == t.id
    assert ev.category == "CI_FAILURE"
    assert ev.normalized_key  # non-empty
    assert "lint" in ev.reason


def test_ci_failure_diagnostic_event_uses_ticket_board_id_fallback(
    tmp_path,
    monkeypatch,
):
    """When ctx.repo_config is None, the emitter falls back to
    ticket.board_id instead of silently skipping the event."""
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events

    ctx = _gh(tmp_path)
    _failing_check_status(monkeypatch)

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    # Simulate _repo_config_for_ticket returning None: wipe repo_config
    # but keep everything else working.
    ctx.repo_config = None

    stage = CIFixStage()
    stage.run(t, ctx)

    # The event should still be emitted because ticket.board_id is
    # available as a fallback.
    events = list_diagnostic_events(ctx.settings, "test-board", category="CI_FAILURE")
    assert len(events) == 1, f"expected 1 CI_FAILURE event, got {len(events)}"
    ev = events[0]
    assert ev.ticket_id == t.id


def test_ci_failure_diagnostic_event_not_emitted_on_check_status_pending(
    tmp_path,
    monkeypatch,
):
    """When check_status returns 'pending' (CI not yet complete), no
    CI_FAILURE event is emitted — the stage returns IMPLEMENT_COMPLETE
    before reaching the emitter."""
    from robotsix_mill.agents.runners.diagnostic_events import list_diagnostic_events

    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "pending",
            "failing": [],
        },
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    stage = CIFixStage()
    out = stage.run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    events = list_diagnostic_events(ctx.settings, "test-board", category="CI_FAILURE")
    assert len(events) == 0, "pending CI should not emit CI_FAILURE event"


# ---------------------------------------------------------------------------
# Merge conflict → REBASING (not BLOCKED)
#
# CI cannot be fixed on a branch that will not merge. The merge stage already
# auto-rebases from human_mr_approval / waiting_auto_merge; ci_fix was the one
# conflict path that demanded a manual rebase, which is what left 10 tickets
# blocked on 2026-08-12 with nothing wrong but a moved target branch.
# ---------------------------------------------------------------------------


def _conflicting_pr(monkeypatch):
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {
            "sha": "abc123",
            "mergeable": False,
            "mergeable_state": "dirty",
        },
    )


def test_merge_conflict_routes_to_rebasing(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    ticket = _fixing_ci(ctx)
    repo_dir = _setup_repo(ctx, ticket)
    _conflicting_pr(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix._detect_merge_conflict",
        lambda *a, **k: "Merge conflict detected — `CHANGELOG.md`",
    )

    stage = CIFixStage()
    outcome = stage._check_merge_conflict(
        ticket, ctx, repo_dir, ticket.branch or "", "main"
    )

    assert outcome is not None
    assert outcome.next_state is State.REBASING
    assert "Merge conflict detected" in (outcome.note or "")


def test_merge_conflict_transition_is_legal(tmp_path):
    """The routing is only useful if the state machine accepts it."""
    from robotsix_mill.core.states import can_transition

    assert can_transition(State.FIXING_CI, State.REBASING) is True


def test_no_merge_conflict_falls_through(tmp_path, monkeypatch):
    """A mergeable PR must not be diverted into the rebase agent."""
    ctx = _gh(tmp_path)
    ticket = _fixing_ci(ctx)
    repo_dir = _setup_repo(ctx, ticket)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {
            "sha": "abc123",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )

    stage = CIFixStage()
    assert (
        stage._check_merge_conflict(ticket, ctx, repo_dir, ticket.branch or "", "main")
        is None
    )


# ---------------------------------------------------------------------------
# Conflicting-PR backstop: a conflicting PR gets zero check runs, so the
# ci-fix agent has nothing to iterate against.
# ---------------------------------------------------------------------------


def _conflicting_ci_fix_ctx(tmp_path, monkeypatch, mergeable):
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch, require_checks=False: {
            "sha": "abc123",
            "mergeable": mergeable,
        },
    )

    def _no_agent(**k):  # pragma: no cover - asserted not to run
        raise AssertionError("ci-fix agent must not run against a conflicting PR")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", _no_agent)
    return ctx


def test_ci_fix_reroutes_conflicting_pr_to_rebasing(tmp_path, monkeypatch):
    """mergeable=False → REBASING without spending an agent iteration."""
    ctx = _conflicting_ci_fix_ctx(tmp_path, monkeypatch, mergeable=False)
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.REBASING
    assert "conflicts" in out.note


@pytest.mark.parametrize("mergeable", [True, None])
def test_ci_fix_runs_agent_when_pr_is_not_conflicting(tmp_path, monkeypatch, mergeable):
    """mergeable=True, and the not-yet-computed None, must NOT divert."""
    ctx = _conflicting_ci_fix_ctx(tmp_path, monkeypatch, mergeable=mergeable)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_ci_fix_mergeability_probe_failure_does_not_divert(tmp_path, monkeypatch):
    """A pr_status blip must fall through to the agent, not strand the ticket."""
    ctx = _conflicting_ci_fix_ctx(tmp_path, monkeypatch, mergeable=False)
    calls = {"n": 0}

    def flaky_pr_status(self, *, source_branch, require_checks=False):
        calls["n"] += 1
        # The first call resolves head_sha; the backstop's probe is the one
        # that fails.
        if calls["n"] > 1:
            raise RuntimeError("transport blip")
        return {"sha": "abc123", "mergeable": False}

    monkeypatch.setattr(github.GitHubForge, "pr_status", flaky_pr_status)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
    )
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


# ---------------------------------------------------------------------------
# Deterministic formatter-only pre-pass (short-circuits the LLM agent)
# ---------------------------------------------------------------------------


_FORMATTER_SUMMARY = "Would reformat: src/a.py\n1 file would be reformatted"


def _formatter_only_check_status(monkeypatch):
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "ci / tests",
                    "summary": _FORMATTER_SUMMARY,
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


def test_formatter_only_failure_short_circuits_agent(tmp_path, monkeypatch):
    """A ruff-format-only failure is fixed deterministically without the agent."""
    ctx = _gh(tmp_path)
    _formatter_only_check_status(monkeypatch)

    def _no_agent(**k):
        raise AssertionError("ci-fix agent must not run for a formatter-only failure")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", _no_agent)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"", b""),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.has_changes", lambda repo: True
    )
    commits: list[str] = []
    pushes: list[str] = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.commit_all",
        lambda repo, msg: commits.append(msg),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda repo, branch, url, token: pushes.append(branch),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert commits and "ruff format" in commits[0]
    assert pushes == [f"mill/{t.id}"]


def test_formatter_pass_no_changes_falls_through_to_agent(tmp_path, monkeypatch):
    """When `ruff format` produces no diff, hand off to the LLM agent."""
    ctx = _gh(tmp_path)
    _formatter_only_check_status(monkeypatch)

    agent_calls = {"n": 0}

    def _agent(**k):
        agent_calls["n"] += 1
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", _agent)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, b"", b""),
    )
    # Formatter changed nothing → do not commit/push, run the agent instead.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.has_changes", lambda repo: False
    )

    def _no_commit(repo, msg):
        raise AssertionError("must not commit when the formatter produced no changes")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.git_ops.commit_all", _no_commit)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert agent_calls["n"] == 1


def test_non_formatter_failure_does_not_run_formatter(tmp_path, monkeypatch):
    """A non-formatter failure must not trigger the deterministic pass."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "ci / tests",
                    "summary": "1 failed, 2 passed\nFAILED tests/test_x.py::test_y",
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

    def _no_format(cmd, **kw):
        raise AssertionError("`ruff format` must not run for a non-formatter failure")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.subprocess.run", _no_format)
    agent_calls = {"n": 0}

    def _agent(**k):
        agent_calls["n"] += 1
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", _agent)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert agent_calls["n"] == 1


# ---------------------------------------------------------------------------
# Non-code-fixable scanner gate + hard total-attempt cap
# ---------------------------------------------------------------------------


def _secret_scan_status(monkeypatch):
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "secret-scan-on-main",
                    "summary": "gitleaks found a secret",
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


def test_secret_scan_failure_blocks_without_running_agent(tmp_path, monkeypatch):
    """A non-code-fixable scanner failure blocks for human remediation and
    never dispatches the (opus) ci-fix agent."""
    ctx = _gh(tmp_path)
    _secret_scan_status(monkeypatch)

    def _agent(**k):
        raise AssertionError("ci-fix agent must not run for a non-code-fixable failure")

    monkeypatch.setattr("robotsix_mill.stages.ci_fix.run_ci_fix_agent", _agent)

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "non-code-fixable" in out.note
    assert "secret-scan-on-main" in out.note


def test_handle_non_code_fixable_returns_none_for_code_check(tmp_path):
    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    failing = [{"name": "ci / tests"}]
    summary = "## ❌ FAILED: ci / tests\n"
    assert (
        CIFixStage()._handle_non_code_fixable_failure(t, ctx, failing, summary) is None
    )


def test_total_attempt_cap_blocks_after_cap(tmp_path):
    ctx = _gh(tmp_path, ci_fix_max_total_attempts=2)
    t = _fixing_ci(ctx)
    stage = CIFixStage()
    summary = "## ❌ FAILED: ci / tests\n"
    assert stage._check_total_attempt_cap(t, ctx, summary) is None  # attempt 1
    assert stage._check_total_attempt_cap(t, ctx, summary) is None  # attempt 2
    out = stage._check_total_attempt_cap(t, ctx, summary)  # attempt 3 → block
    assert out is not None
    assert out.next_state is State.BLOCKED
    assert "hard attempt cap" in out.note


def test_total_attempt_cap_resets_when_checkset_changes(tmp_path):
    ctx = _gh(tmp_path, ci_fix_max_total_attempts=1)
    t = _fixing_ci(ctx)
    stage = CIFixStage()
    s1 = "## ❌ FAILED: check-a\n"
    s2 = "## ❌ FAILED: check-b\n"
    assert stage._check_total_attempt_cap(t, ctx, s1) is None  # a: attempt 1
    out = stage._check_total_attempt_cap(t, ctx, s1)  # a: attempt 2 > cap
    assert out is not None and out.next_state is State.BLOCKED
    # A different failing check-set is genuine progress → budget resets.
    assert stage._check_total_attempt_cap(t, ctx, s2) is None  # b: attempt 1


def test_total_attempt_cap_disabled(tmp_path):
    ctx = _gh(tmp_path, ci_fix_max_total_attempts=0)
    t = _fixing_ci(ctx)
    stage = CIFixStage()
    summary = "## ❌ FAILED: ci / tests\n"
    for _ in range(5):
        assert stage._check_total_attempt_cap(t, ctx, summary) is None


def test_infra_account_block_parks_without_running_agent(tmp_path, monkeypatch):
    """GitHub account/billing block → BLOCKED with the marker, no agent run.

    The failing run's checks carry the billing annotation; the gate must
    park the ticket immediately and never reach ``_run_agent_and_finalize``
    (the whole cost saving — no implement-class session on an unfixable
    condition).
    """
    from robotsix_mill.stages.ci_fix_helpers import _FailingContext
    from robotsix_mill.stages.ci_infra_block import INFRA_ACCOUNT_BLOCK_MARKER

    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    stage = CIFixStage()

    billing = (
        "The job was not started because recent account payments have "
        "failed or your spending limit needs to be increased."
    )
    fctx = _FailingContext(
        repo_dir=str(tmp_path),
        branch=t.branch,
        failing_summary="CI\n" + billing,
        failing=[
            {
                "name": "CI",
                "summary": None,
                "text": None,
                "annotations": [{"message": billing, "level": "failure"}],
            }
        ],
    )
    monkeypatch.setattr(stage, "_resolve_clone_and_status", lambda ticket, c: fctx)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix._emit_ci_failure_event",
        lambda *a, **k: None,
    )
    # Target branch is green (upstream check returns None) so we reach the gate.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix._check_upstream_ci_breakage",
        lambda *a, **k: None,
    )

    def _boom(*a, **k):
        raise AssertionError("ci_fix agent must NOT run for an account block")

    monkeypatch.setattr(stage, "_run_agent_and_finalize", _boom)

    out = stage.run(t, ctx)

    assert out.next_state is State.BLOCKED
    assert INFRA_ACCOUNT_BLOCK_MARKER in (out.note or "")
