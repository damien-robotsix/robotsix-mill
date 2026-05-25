"""Tests for the CIFixStage (FIXING_CI → HUMAN_MR_APPROVAL | BLOCKED)."""

import pytest

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.ci_fix import (
    CIFixStage,
    _read_counter,
    _write_counter,
    _build_failing_summary,
)
from robotsix_mill.agents.ci_fixing import CiFixResult


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("MILL_DATA_DIR", str(tmp_path / "data"))
    s = Settings(**env)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.get("FORGE_TOKEN")
    if ft is not None:
        from robotsix_mill.config import Secrets, _reset_secrets
        import robotsix_mill.config as _cfg
        _reset_secrets()
        _cfg._secrets = Secrets(forge_token=ft)
    db.init_db(s)
    from robotsix_mill.config import RepoConfig; return StageContext(settings=s, service=TicketService(s), repo_config=RepoConfig(repo_id="test-repo", board_id="test-board", langfuse_project_name="test", langfuse_public_key="pk-test", langfuse_secret_key="sk-test"))


def _fixing_ci(ctx):
    t = ctx.service.create("x", "y")
    for st in (State.READY, State.DELIVERABLE, State.HUMAN_MR_APPROVAL, State.FIXING_CI):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _gh(tmp_path, **extra):
    return _ctx(
        tmp_path, FORGE_KIND="github", FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git", **extra,
    )


def _setup_repo(ctx, ticket):
    """Create a minimal .git in the workspace so _workspace_repo_dir succeeds."""
    repo_dir = ctx.service.workspace(ticket).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)
    return str(repo_dir)


# --- E.27: Fix success + push success → HUMAN_MR_APPROVAL ---

def test_fix_success_push_success_returns_human_mr_approval(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "lint", "summary": "err", "text": None, "annotations": []}],
        },
    )
    # pr_status is called to get head_sha for job-log fetching.
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
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
        "robotsix_mill.stages.ci_fix.git_ops.push", fake_push,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert push_seen["branch"] == f"mill/{t.id}"

    # Counter reset to 0.
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_attempts.txt"
    assert _read_counter(counter) == 0


# --- E.28: Fix success + push failure → BLOCKED ---

def test_fix_success_push_failure_blocks(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "lint", "summary": None, "text": None, "annotations": []}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
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


# --- E.29: Fix failure, attempts remaining → HUMAN_MR_APPROVAL ---

def test_fix_failure_retries_next_poll(tmp_path, monkeypatch):
    ctx = _gh(tmp_path, MILL_CI_FIX_MAX_ATTEMPTS="3")
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "test", "summary": None, "text": None, "annotations": []}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
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

    # Attempt 1: fails → HUMAN_MR_APPROVAL, counter=1
    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.HUMAN_MR_APPROVAL
    assert _read_counter(counter) == 1
    assert push_calls == []  # never pushed on failure


# --- E.30: Fix failure, attempts exhausted → BLOCKED ---

def test_fix_failure_exhausted_blocks(tmp_path, monkeypatch):
    ctx = _gh(tmp_path, MILL_CI_FIX_MAX_ATTEMPTS="2")
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "test", "summary": None, "text": None, "annotations": []}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
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

    # Attempt 1: fails → HUMAN_MR_APPROVAL
    out1 = CIFixStage().run(t, ctx)
    assert out1.next_state is State.HUMAN_MR_APPROVAL

    # Attempt 2: fails → BLOCKED (exhausted)
    out2 = CIFixStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert "ci fix failed after 2 attempt" in out2.note

    # Counter reset on exhaustion.
    assert _read_counter(counter) == 0


# --- E.31: Agent crash → treated as failure ---

def test_agent_crash_treated_as_failure(tmp_path, monkeypatch):
    ctx = _gh(tmp_path, MILL_CI_FIX_MAX_ATTEMPTS="1")
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "lint", "summary": None, "text": None, "annotations": []}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
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


# --- E.32: Missing workspace clone → BLOCKED ---

def test_missing_workspace_clone_blocks(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    # No repo dir created.

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "workspace clone is missing" in out.note


# --- E.33: Forge not configured → BLOCKED ---

def test_forge_not_configured_blocks(tmp_path):
    ctx = _ctx(tmp_path)
    out = CIFixStage().run(_fixing_ci(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "forge not configured" in out.note


# --- E.34: Force-push refspec is ticket branch only ---

def test_force_push_refspec_is_ticket_branch_only(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "lint", "summary": None, "text": None, "annotations": []}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
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


# --- CI green/pending while in FIXING_CI → back to HUMAN_MR_APPROVAL ---

def test_ci_green_while_in_fixing_ci_returns_human_mr_approval(tmp_path, monkeypatch):
    """If CI turns green while we're in FIXING_CI, go back to HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_ci_pending_while_in_fixing_ci_returns_human_mr_approval(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_check_status_returns_none_while_in_fixing_ci(tmp_path, monkeypatch):
    """PR disappeared → back to HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: None,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_check_status_exception_while_in_fixing_ci(tmp_path, monkeypatch):
    """Transient error → back to HUMAN_MR_APPROVAL for re-poll."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("api down")),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


# --- Counter location ---

def test_counter_location_is_artifacts_dir(tmp_path, monkeypatch):
    """E.36: Counter is at artifacts_dir / ci_fix_attempts.txt."""
    ctx = _gh(tmp_path, MILL_CI_FIX_MAX_ATTEMPTS="3")
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "test", "summary": None, "text": None, "annotations": []}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_status",
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
                {"path": "src/foo.py", "start_line": 10, "message": "unused import os", "level": "failure"},
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
        github.GitHubForge, "pr_status",
        lambda self, *, source_branch: {
            "merged": False, "state": "open", "url": "http://pr",
            "mergeable": True, "sha": "abc123",
        },
    )
    # check_status returns failure.
    monkeypatch.setattr(
        github.GitHubForge, "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "build", "summary": None, "text": None, "annotations": []}],
        },
    )
    # list_workflow_runs returns one failed run.
    monkeypatch.setattr(
        github.GitHubForge, "list_workflow_runs",
        lambda self, *, branch=None, head_sha=None: [
            {"id": 42, "name": "CI", "workflow_id": 100,
             "head_sha": "abc123", "conclusion": "failure",
             "html_url": "http://x", "created_at": "2025-01-01T00:00:00Z"},
        ],
    )
    # fetch_workflow_job_logs returns log text.
    monkeypatch.setattr(
        github.GitHubForge, "fetch_workflow_job_logs",
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
    assert out.next_state is State.HUMAN_MR_APPROVAL
