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
from robotsix_mill.stages.ci_fix import CIFixStage
from robotsix_mill.stages.ci_fix_helpers import (
    _build_failing_summary,
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


def test_out_of_scope_dedups_across_parent_tickets(tmp_path, monkeypatch):
    """Parents failing on the same repo-level check share ONE fix ticket.

    Regression (2026-09-01): the dedup fingerprint hashed the raw summary
    (embedding per-branch head_sha/URLs) and the title embedded LLM
    wording, so six parents parked on one repo-wide parse failure each
    spawned their own duplicate dependency ticket.  The fingerprint and
    title now derive from the failing check names, which are identical
    across parents regardless of branch head or verdict wording.
    """
    ctx = _gh(tmp_path)
    _oos_forge(monkeypatch)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.git_ops.push_with_lease",
        lambda *a, **k: None,
    )

    t1 = _fixing_ci(ctx)
    _setup_repo(ctx, t1)
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(),
    )
    out1 = CIFixStage().run(t1, ctx)
    assert out1.next_state is State.BLOCKED

    # Second parent: different branch head, different LLM wording.
    t2 = _fixing_ci(ctx)
    _setup_repo(ctx, t2)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch, require_checks=False: {"sha": "def456"},
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.ci_fix.run_ci_fix_agent",
        lambda **k: _oos_result(
            failing_check="the CodeQL clear-text-logging alert",
            required_change_area="the shared logging helper (already correct here)",
        ),
    )
    out2 = CIFixStage().run(t2, ctx)
    assert out2.next_state is State.BLOCKED

    fixes = ctx.service.recent_proposals_for(SourceKind.CI_FIX_DEPENDENCY)
    assert len(fixes) == 1
    # Deterministic title from the check names, not the LLM verdict wording.
    assert "CodeQL" in fixes[0].title
    assert "shared logging helper" not in fixes[0].title
    # Both parents auto-resume when the single fix ticket completes.
    assert set(json.loads(fixes[0].unblocks)) == {t1.id, t2.id}


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
