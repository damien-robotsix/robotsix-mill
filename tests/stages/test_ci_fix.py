"""Tests for the CIFixStage (FIXING_CI → IMPLEMENT_COMPLETE | BLOCKED)."""

import json

from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.models import SourceKind
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.vcs import git_ops
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.ci_fix import CIFixStage
from robotsix_mill.stages.ci_fix_helpers import (
    _build_failing_summary,
    _ci_failure_fingerprint,
    _format_alert_summary_block,
    _partition_alerts_by_diff,
    _read_counter,
    _write_counter,
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


def _failing_check_status(monkeypatch):
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
    # 274d: 16x unused-global + 4x empty-except, ALL in the PR's added files.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: (
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
        lambda self, *, source_branch: [
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
    fp = _ci_failure_fingerprint(summary, repo_id)
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
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "new err", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
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
    old_fp = _ci_failure_fingerprint(old_summary, repo_id)
    (artifacts / "ci_failure_fingerprint.txt").write_text(old_fp, encoding="utf-8")

    # Current failure is "lint" (different from "pytest" in the stored FP).
    failing = [{"name": "lint", "summary": "new err", "text": None, "annotations": []}]
    current_summary = _build_failing_summary(failing)
    current_fp = _ci_failure_fingerprint(current_summary, repo_id)
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
        lambda self, *, source_branch: {
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
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    # Return a security-severity alert (high).
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [
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
        lambda self, *, source_branch: [
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
    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)
    branch = f"mill/{t.id}"
    stage = CIFixStage()

    # success
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    assert stage._make_ci_status_fn(t, ctx, branch)() == ("success", "")

    # pending
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )
    assert stage._make_ci_status_fn(t, ctx, branch)() == ("pending", "")

    # gone (PR vanished)
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: None,
    )
    assert stage._make_ci_status_fn(t, ctx, branch)() == ("gone", "")

    # failure carries a summary
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": "boom", "text": None, "annotations": []}
            ],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    conclusion, summary = stage._make_ci_status_fn(t, ctx, branch)()
    assert conclusion == "failure"
    assert "lint" in summary


def test_transient_check_status_error_maps_to_pending(tmp_path, monkeypatch):
    """A forge exception during the wait probe maps to 'pending' so the agent
    keeps waiting rather than giving up on a blip."""
    ctx = _gh(tmp_path)
    t = _fixing_ci(ctx)
    _setup_repo(ctx, t)

    def boom(self, *, source_branch):
        raise RuntimeError("forge 500")

    monkeypatch.setattr(github.GitHubForge, "check_status", boom)
    assert CIFixStage()._make_ci_status_fn(t, ctx, f"mill/{t.id}")() == ("pending", "")


def test_branch_own_failure_goes_straight_to_agent(tmp_path, monkeypatch):
    """A branch-own CI failure runs the ci-fix agent on the FIRST cycle —
    no proactive rebase precedes it (regression for the rebase-starvation
    that left trivial branch-own lint/vulture fixes stuck; ticket c14c)."""
    ctx = _gh(tmp_path)
    _failing_check_status(monkeypatch)
    # Even when the branch is behind main, there must be no proactive rebase:
    # the agent owns the fix and rebases itself only if it decides to.
    rebase_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.try_rebase_onto",
        lambda *a, **k: rebase_calls.append(1) or True,
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
    assert rebase_calls == [], "no proactive rebase before the agent"


# ---------------------------------------------------------------------------
# Artifact + history note observability
# ---------------------------------------------------------------------------


def test_failing_summary_txt_written_on_failure(tmp_path, monkeypatch):
    """failing_summary.txt is written (non-empty) when CI is detected failing."""
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
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": "abc123"},
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
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "build", "summary": None, "text": None, "annotations": []}
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
        lambda self, *, source_branch: {
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
        lambda self, *, source_branch: {"sha": "abc123"},
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
        lambda self, *, source_branch: {
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
        lambda self, *, source_branch: {"sha": "abc123"},
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
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
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
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
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
        lambda self, *, source_branch: None,
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
        lambda self, *, source_branch: {
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
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    # list_code_scanning_alerts raises the 403 signal.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: (_ for _ in ()).throw(
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
        lambda self, *, source_branch: {
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
        lambda self, *, source_branch: {"sha": "abc123"},
    )
    # Return a security-severity alert (high) — the normal readable path.
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [
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
        lambda self, *, source_branch: [
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
