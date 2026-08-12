"""Tests for the CIFixStage (FIXING_CI → IMPLEMENT_COMPLETE | BLOCKED)."""

import json

import pytest

from robotsix_mill.agents.ci_fixing import CiFixResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.ci_fix import CIFixStage, _extract_check_names
from robotsix_mill.stages.ci_fix_helpers import (
    _build_failing_summary,
    _ci_failure_fingerprint,
    _format_alert_summary_block,
    _partition_alerts_by_diff,
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


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    s = Settings(**env)
    # Mirror forge_token into Secrets so get_secrets() works
    ft = env.get("FORGE_TOKEN")
    if ft is not None:
        import robotsix_mill.config as _cfg
        from robotsix_mill.config import Secrets, _reset_secrets

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

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "push did not land" in out.note


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
    assert "## ❌ FAILED: lint" in result


def test_ci_fix_stage_fetches_job_logs_on_failure(tmp_path, monkeypatch):
    """Mock list_workflow_runs + fetch_workflow_job_logs; verify
    _build_failing_summary receives the log text."""
    ctx = _gh(tmp_path)
    # PR status returns a sha.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {
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
        lambda self, *, source_branch, require_checks=False: {
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
    # push succeeds via post_push_check.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: git_ops.PostPushResult.PASS,
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
    assert "py/x" in out
    assert "t.py:9" in out
    assert "high" in out


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


# ---------------------------------------------------------------------------
# _format_alert_summary_block — fail-loud on empty CodeQL
# ---------------------------------------------------------------------------


def test_format_alert_summary_block_empty_codeql_failing_emits_notice():
    """When CodeQL is failing and alerts are empty, emit a could-not-retrieve
    notice instead of a silent empty string."""
    result = _format_alert_summary_block(None, codeql_failing=True)
    assert "could not be retrieved" in result
    assert "code-scanning API" in result


def test_format_alert_summary_block_empty_no_codeql_returns_empty():
    """When CodeQL is not the only failing check, empty alerts still return
    an empty string (backward-compatible)."""
    assert _format_alert_summary_block([]) == ""
    assert _format_alert_summary_block(None) == ""
    assert _format_alert_summary_block([], codeql_failing=False) == ""


def test_build_failing_summary_codeql_failing_no_alerts():
    """Full integration: when every failing check is CodeQL but alerts are
    empty, the fail-loud notice appears in _build_failing_summary output."""
    out = _build_failing_summary(
        failing=[{"name": "CodeQL / Analyze (python)"}],
        log_text="",
        alerts=[],
    )
    assert "could not be retrieved" in out
    assert "code-scanning API" in out


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
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: push_calls.append(1),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY) == []
    assert push_calls == []


def test_alerts_in_added_files_classify_in_scope_no_spawn(tmp_path, monkeypatch):
    """274d's exact shape: every CodeQL alert lives in a file the PR ADDED
    (pr_files status='added'). _pr_changed_paths keeps added files, so the
    alerts classify in-scope → no CI_FIX_DEPENDENCY fixer is spawned, the
    agent's OUT_OF_SCOPE verdict is overridden back to IMPLEMENT_COMPLETE,
    and the branch is never pushed."""
    ctx = _gh(tmp_path)
    added = "src/pkg/new_mod.py"
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
    # 274d: 16x unused-global + 4x empty-except, ALL in the PR's added files.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: (
            [
                {
                    "rule": "py/unused-global-variable",
                    "severity": "warning",
                    "path": added,
                    "line": i,
                    "message": "unused global",
                }
                for i in range(16)
            ]
            + [
                {
                    "rule": "py/empty-except",
                    "severity": "warning",
                    "path": added,
                    "line": 100 + i,
                    "message": "empty except",
                }
                for i in range(4)
            ]
        ),
    )
    # The alert file is an ADDED file in the PR (status='added').
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch, require_checks=False: [
            {"path": added, "status": "added", "additions": 40, "deletions": 0}
        ],
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    push_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
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
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
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
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
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
    # depends_on is cleared after spawn_dependency_fix so the operator's
    # resume-blocked is not blocked by the dependency check in
    # _process_ticket_inner.  The unblocks relationship on the fix ticket
    # is sufficient for auto-resume.
    orig = ctx.service.get(t.id)
    assert (orig.depends_on or "") == "" or json.loads(orig.depends_on) == []
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
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
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
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
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
    """Regression: an in-scope DONE verdict still push-checks and returns
    IMPLEMENT_COMPLETE without spawning any out-of-scope fix ticket."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: CiFixResult(status="DONE", summary="fixed"),
    )
    post_check_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: (
            post_check_calls.append(1) or git_ops.PostPushResult.PASS
        ),
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert post_check_calls == [1]
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
        lambda self, *, source_branch, require_checks=False: {
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
        lambda self, *, source_branch, require_checks=False: {
            "sha": "abc123",
            "mergeable": True,
            "mergeable_state": "behind",
        },
    )
    update_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch, require_checks=False: (
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
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
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
        lambda self, *, source_branch, require_checks=False: {
            "sha": "abc123",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )
    update_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch, require_checks=False: (
            update_calls.append(source_branch) or {"updated": True}
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
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
        lambda self, *, source_branch, require_checks=False: {
            "sha": "abc123",
            "mergeable": True,
            "mergeable_state": "behind",
        },
    )
    update_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch, require_checks=False: (
            update_calls.append(source_branch) or {"updated": True}
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
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


# ---------------------------------------------------------------------------
# CI-failure fingerprint
# ---------------------------------------------------------------------------


def test_ci_failure_fingerprint_is_stable() -> None:
    """Same failing_summary + repo_id always produces the same fingerprint."""
    summary = (
        "## Failing check #1: lint / ruff\n"
        "**Summary:**\nFound 3 errors\n\n"
        "**Job logs:**\n```\n(timestamp: 2025-06-14T12:00:00Z)\n"
        "error: unused import\n```\n"
    )
    fp1 = _ci_failure_fingerprint(summary, "test-board")
    fp2 = _ci_failure_fingerprint(summary, "test-board")
    assert fp1 == fp2
    assert len(fp1) == 16
    # All hex chars.
    assert all(c in "0123456789abcdef" for c in fp1)


def test_ci_failure_fingerprint_differs_for_different_checks() -> None:
    """Different failing check names produce different fingerprints."""
    s1 = "## Failing check #1: lint\n**Summary:**\nerror\n\n**Job logs:**\n```\nlog\n```\n"
    s2 = "## Failing check #1: pytest\n**Summary:**\nerror\n\n**Job logs:**\n```\nlog\n```\n"
    fp1 = _ci_failure_fingerprint(s1, "board")
    fp2 = _ci_failure_fingerprint(s2, "board")
    assert fp1 != fp2


def test_ci_failure_fingerprint_differs_for_different_repos() -> None:
    """Same failure on different repos produces different fingerprints."""
    summary = (
        "## Failing check #1: lint\n**Summary:**\nerror\n\n**Job logs:**\n```\nx\n```\n"
    )
    fp1 = _ci_failure_fingerprint(summary, "board-a")
    fp2 = _ci_failure_fingerprint(summary, "board-b")
    assert fp1 != fp2


def test_ci_failure_fingerprint_truncates_at_job_logs_marker() -> None:
    """The **Job logs:** marker and everything after is excluded from the hash."""
    base = "## Failing check #1: lint\n**Summary:**\nerror\n\n"
    s1 = base + "**Job logs:**\n```\nlog-v1\n```\n"
    s2 = base + "**Job logs:**\n```\nlog-v2-different-timestamps\n```\n"
    assert _ci_failure_fingerprint(s1, "b") == _ci_failure_fingerprint(s2, "b")


def test_ci_failure_fingerprint_truncates_at_2000_chars_when_no_marker() -> None:
    """Without a **Job logs:** marker, the input is truncated to 2000 chars."""
    # Build a summary > 2000 chars with no marker.
    prefix = "## Failing check #1: lint\n**Summary:**\n" + ("x" * 3000)
    suffix = "\nmore stuff that differs"
    s1 = prefix + suffix
    s2 = prefix + "-different-suffix"
    # Both share the same first 2000 chars → same fingerprint.
    assert _ci_failure_fingerprint(s1, "b") == _ci_failure_fingerprint(s2, "b")


def test_ci_failure_fingerprint_empty_summary() -> None:
    """Empty failing_summary produces a valid fingerprint (does not crash)."""
    fp = _ci_failure_fingerprint("", "board")
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_ci_failure_fingerprint_passed_to_spawn_via_dedup_labels(
    tmp_path, monkeypatch
) -> None:
    """When _handle_out_of_scope runs, it computes a fingerprint and passes
    dedup_labels=[ci_fp:<hex>] to spawn_dependency_fix."""
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )
    # Capture the call to spawn_dependency_fix.
    spawn_kwargs = {}

    def fake_spawn(ticket, ctx, **kwargs):
        spawn_kwargs.update(kwargs)
        # Return a valid Outcome so the stage doesn't crash.
        from robotsix_mill.stages.base import Outcome

        return Outcome(State.BLOCKED, "test")

    monkeypatch.setattr(
        "robotsix_mill.stages.dependency_fix.spawn_dependency_fix",
        fake_spawn,
    )

    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    CIFixStage().run(t, ctx)

    assert "dedup_labels" in spawn_kwargs
    labels = spawn_kwargs["dedup_labels"]
    assert len(labels) == 1
    assert labels[0].startswith("ci_fp:")
    assert len(labels[0]) == len("ci_fp:") + 16  # "ci_fp:" + 16 hex chars


# ---------------------------------------------------------------------------
# _extract_check_names — parse check names from the failing summary format
# ---------------------------------------------------------------------------


def test_extract_check_names_empty_or_none() -> None:
    """Empty string or whitespace-only returns (unknown)."""
    assert _extract_check_names("") == "(unknown)"
    assert _extract_check_names("   \n  ") == "(unknown)"


def test_extract_check_names_single_failing() -> None:
    """A single ❌ FAILED: header returns the check name."""
    summary = "## ❌ FAILED: ruff / lint\n\n**Summary:**\nFound 3 errors\n"
    assert _extract_check_names(summary) == "ruff / lint"


def test_extract_check_names_multiple_failing() -> None:
    """Multiple ❌ FAILED: headers return a comma-separated list."""
    summary = (
        "## ❌ FAILED: ruff / lint\n\n"
        "**Summary:**\nFound 3 errors\n\n"
        "## ✅ PASSED: tests\n\n"
        "## ❌ FAILED: typecheck (3.12)\n\n"
        "**Details:**\n...\n"
    )
    result = _extract_check_names(summary)
    # Order is discovery order (top-down).
    assert "ruff / lint" in result
    assert "typecheck (3.12)" in result
    assert result == "ruff / lint, typecheck (3.12)"


def test_extract_check_names_skips_passed() -> None:
    """✅ PASSED: headers are not collected."""
    summary = "## ✅ PASSED: tests\n\n## ✅ PASSED: lint\n"
    assert _extract_check_names(summary) == "(unknown)"


def test_extract_check_names_codeql_compact_block() -> None:
    """A compact CodeQL alert block yields 'CodeQL code-scanning'."""
    summary = (
        "**CodeQL alerts to fix (extracted for fast reference — rule ID and location):**\n"
        "- `py/clear-text-logging` @ src/foo.py:42\n\n"
        "## ❌ FAILED: CodeQL\n\n"
        "**Summary:**\n...\n"
    )
    result = _extract_check_names(summary)
    # Both the compact block and the failing header are collected; the
    # compact block comes first.
    assert "CodeQL code-scanning" in result
    assert "CodeQL" in result


def test_extract_check_names_collects_all_failing_headers() -> None:
    """All ❌ FAILED: headers are collected regardless of intermediate content."""
    summary = (
        "## ❌ FAILED: lint\n\n**Summary:**\nSome error\n\n## ❌ FAILED: late_check\n\n"
    )
    result = _extract_check_names(summary)
    assert result == "lint, late_check"


def test_extract_check_names_truncates() -> None:
    """Result is truncated to 200 characters."""
    # Build check names that together exceed 200 chars.
    long_name = "very-long-check-name-" + ("x" * 180)
    summary = f"## ❌ FAILED: {long_name}\n\n**Summary:**\n...\n"
    result = _extract_check_names(summary)
    assert len(result) <= 200
    assert result.startswith("very-long-check-name")


def test_extract_check_names_realistic_mixed_summary() -> None:
    """Integration-style test with a realistic mixed pass/fail summary."""
    summary = _build_failing_summary(
        [
            {"name": "ruff / lint", "conclusion": "failure", "summary": "3 errors"},
            {"name": "tests (3.12)", "conclusion": "success", "summary": "42 passed"},
            {"name": "typecheck", "conclusion": "failure", "summary": "1 error"},
        ]
    )
    result = _extract_check_names(summary)
    assert "ruff / lint" in result
    assert "typecheck" in result
    assert "tests (3.12)" not in result


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


def test_ci_status_fn_passed_to_agent(tmp_path, monkeypatch):
    """The stage wires a host-side ci_status_fn into the agent so its
    wait_for_ci tool can probe the forge."""
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
    assert callable(captured["ci_status_fn"])


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


# ---------------------------------------------------------------------------
# Duplicate changelog fragment dedup (before LLM agent)
# ---------------------------------------------------------------------------


def test_ci_fix_dedup_removes_extra_fragment_without_invoking_agent(
    tmp_path, monkeypatch
):
    """When the PR branch carries two ``changes/<id>.<type>.md`` files, the
    dedup early-path removes the lower-priority fragment, commits, pushes,
    and returns IMPLEMENT_COMPLETE — without invoking the LLM agent."""
    from pathlib import Path

    ctx = _gh(tmp_path)
    # CI must be failing for the flow to reach the dedup gate.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "failure",
            "failing": [
                {
                    "name": "ci / tests",
                    "summary": "dup fragment",
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
    # Keep enrichment seams quiet.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch, require_checks=False: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_workflow_runs",
        lambda self, *, head_sha=None, branch=None: [],
    )

    t = _fixing_ci(ctx)
    repo_dir = _setup_repo(ctx, t)

    # Create the duplicate towncrier fragments.
    changes_dir = Path(repo_dir) / "changes"
    changes_dir.mkdir()
    (changes_dir / f"{t.id}.feature.md").write_text(
        "New feature description", encoding="utf-8"
    )
    (changes_dir / f"{t.id}.misc.md").write_text("My ticket title", encoding="utf-8")

    # Write pyproject.toml with towncrier config.
    (Path(repo_dir) / "pyproject.toml").write_text(
        '[tool.towncrier]\ndirectory = "changes"\n',
        encoding="utf-8",
    )

    # Mock has_changes → True so the dedup path commits.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.has_changes",
        lambda repo: True,
    )

    # Record commit_all calls.
    commit_calls = []

    def fake_commit_all(repo, message):
        commit_calls.append((str(repo), message))

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.commit_all",
        fake_commit_all,
    )

    # Record push calls.
    push_calls = []

    def fake_push(repo, branch, remote_url, token):
        push_calls.append((str(repo), branch, remote_url, token))

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push",
        fake_push,
    )

    # Mock credential resolution.
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix._resolve_remote_url",
        lambda s, rc: "https://example.com/repo.git",
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.github_token",
        lambda *args, **kwargs: "tok",
    )

    # Guard: the LLM agent must NOT be invoked.
    def boom_agent(**k):
        raise AssertionError("agent must not be invoked for duplicate-fragment fix")

    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        boom_agent,
    )

    out = CIFixStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # .misc.md must no longer exist on disk.
    assert not (changes_dir / f"{t.id}.misc.md").exists()

    # .feature.md must still exist (higher priority).
    assert (changes_dir / f"{t.id}.feature.md").exists()

    # commit_all was called exactly once.
    assert len(commit_calls) == 1

    # push was called exactly once with the ticket branch.
    assert len(push_calls) == 1
    assert push_calls[0][1] == f"mill/{t.id}"


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


def test_agent_crash_without_timeout_uses_generic_note(tmp_path, monkeypatch):
    """When _invoke_agent returns None but _last_agent_timed_out is False
    (agent crashed, not timed out), the generic budget-exhausted note is
    used instead of the timeout diagnostic."""
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
    # The generic budget note, not the timeout diagnostic.
    assert "iteration budget" in out.note
    assert "timed out" not in out.note


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
