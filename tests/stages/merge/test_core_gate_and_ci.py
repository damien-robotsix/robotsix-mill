"""Merge-stage core tests: IMPLEMENT_COMPLETE gate checks, CI branching,
HUMAN_MR_APPROVAL handling and the review-feedback gate."""

import json

import pytest

from robotsix_mill.core.states import State
from robotsix_mill.scm.forge import github
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.merge import MergeStage
from tests.stages.merge.conftest import (
    _ctx,
    _gh,
    _human_mr_approval,
    _implement_complete,
    _in_rebasing,
    _write_review_artifact,
)

# ============================================================
# IMPLEMENT_COMPLETE gate-check poll path (new)
# ============================================================


def test_implement_complete_ci_green_mergeable_promotes_to_human_mr_approval(
    tmp_path, monkeypatch
):
    """CI green + PR mergeable → HUMAN_MR_APPROVAL (gates passed)."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    t = _implement_complete(ctx)
    # Pre-seed a non-zero ci_fix cycle counter (as a prior ci_fix loop would).
    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_cycles.txt"
    cycle_path.parent.mkdir(parents=True, exist_ok=True)
    cycle_path.write_text("2", encoding="utf-8")

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    # The primary MR-approval transition records an operator-facing reason.
    assert out.note
    assert "mergeable" in out.note
    # Genuine forward progress (gates passed) resets the ci_fix cycle ceiling.
    assert cycle_path.read_text().strip() == "0"


def test_implement_complete_ci_failing_transitions_to_fixing_ci(tmp_path, monkeypatch):
    """CI failing → FIXING_CI."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.FIXING_CI


def _ci_failing_mergeable(monkeypatch):
    """Patch the forge so the PR is open+mergeable with failing CI."""
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


def test_implement_complete_ci_failing_behind_main_goes_to_ci_fix(
    tmp_path, monkeypatch
):
    """CI failing + branch behind main → FIXING_CI (NOT REBASING).

    Branch-introduced failures (those green on current main) go straight to
    ci_fix — rebasing cannot fix a branch's own lint/type failure and just
    churns under a fast-moving main. The branch gets made current with main
    via the single rebase-and-merge at the end of the merge stage."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch)
    # Workspace clone present + behind main.
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": True,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI


def test_implement_complete_ci_failing_up_to_date_goes_to_ci_fix(tmp_path, monkeypatch):
    """CI failing + branch NOT behind main → FIXING_CI (genuine failure;
    a rebase would be a no-op, so don't loop)."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    _ci_failing_mergeable(monkeypatch)
    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: "/repo")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "branch_is_behind_main",
        lambda repo, target_branch="main": False,
    )
    out = MergeStage().run(_implement_complete(ctx), ctx)
    assert out.next_state is State.FIXING_CI


def test_implement_complete_conflicting_transitions_to_rebasing(tmp_path, monkeypatch):
    """PR conflicting → REBASING."""
    ctx = _gh(tmp_path)
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
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING


def test_implement_complete_ci_pending_stays_same_state(tmp_path, monkeypatch):
    """CI pending → same-state IMPLEMENT_COMPLETE (re-poll)."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_implement_complete_no_check_status_stays_same_state(tmp_path, monkeypatch):
    """check_status returns None → same-state IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: None,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_implement_complete_merged_transitions_to_done(tmp_path, monkeypatch):
    """PR merged while polling → DONE."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "https://gh/o/r/pull/3",
        },
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_implement_complete_closed_unmerged_blocks(tmp_path, monkeypatch):
    """PR closed unmerged → BLOCKED."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u",
        },
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED


def test_implement_complete_pr_status_none_stays_same_state(tmp_path, monkeypatch):
    """pr_status returns None → same-state IMPLEMENT_COMPLETE (re-poll)."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: None,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_implement_complete_transient_error_stays_same_state(tmp_path, monkeypatch):
    """pr_status raises → same-state IMPLEMENT_COMPLETE (re-poll)."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("api down")),
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_implement_complete_check_status_transient_error_stays_same_state(
    tmp_path, monkeypatch
):
    """check_status raises → same-state IMPLEMENT_COMPLETE (re-poll)."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("api down")),
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


# --- existing paths (updated for IMPLEMENT_COMPLETE) ---


def test_blocked_when_forge_unconfigured(tmp_path):
    ctx = _ctx(tmp_path)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
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
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    # Should NOT block due to forge_kind=none. May fail for other
    # reasons (e.g. no PR found, forge unreachable), but the note must
    # not contain the "forge not configured" sentinel (and the state
    # must not be BLOCKED for that reason).
    assert out.next_state is not State.BLOCKED or (
        out.note is not None and "forge not configured" not in out.note
    )


def test_merged_to_done(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "https://github.com/o/r/pull/3",
        },
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "pull/3" in out.note
    assert (ctx.service.workspace(t).artifacts_dir / "merge.md").exists()


def test_closed_unmerged_blocks(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u",
        },
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert "closed without merge" in out.note


def _seed_workspace_clone(ctx, t, *, net_diff: bool) -> None:
    """Build the ticket's workspace clone (``ws.dir/repo``) from a bare
    remote with a ``mill/<id>`` branch. When *net_diff* is False the branch
    is identical to origin/main (empty-after-rebase); when True it carries
    a real change."""
    import subprocess as _sp

    from robotsix_mill.scm.vcs import git_ops

    tmp = ctx.service.workspace(t).dir
    seed = tmp / "seed"
    seed.mkdir(parents=True)

    def _g(cwd, *args):
        _sp.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)

    _g(seed, "init", "-q")
    _g(seed, "config", "user.email", "t@t")
    _g(seed, "config", "user.name", "t")
    (seed / "README.md").write_text("seed\n")
    _g(seed, "add", "-A")
    _g(seed, "commit", "-q", "-m", "init")
    _g(seed, "branch", "-M", "main")
    bare = tmp / "remote.git"
    _sp.run(
        ["git", "clone", "--bare", "-q", str(seed), str(bare)],
        check=True,
        capture_output=True,
    )
    repo = ctx.service.workspace(t).repo_dir
    git_ops.clone(f"file://{bare}", repo, "main")
    branch = f"mill/{t.id}"
    git_ops.create_branch(repo, branch)
    if net_diff:
        (repo / "change.txt").write_text("real change\n")
        git_ops.commit_all(repo, "real work")
    ctx.service.set_branch(t.id, branch)


def test_closed_unmerged_empty_branch_terminates_done(tmp_path, monkeypatch):
    """A PR closed without merge whose branch has NO net diff vs the
    target (empty-after-rebase: main already carries the change) is a
    genuine no-op → DONE, not a BLOCKED-resume loop (ticket 0976)."""
    ctx = _gh(tmp_path, delete_branch_on_merge=False)
    t = _human_mr_approval(ctx)
    _seed_workspace_clone(ctx, t, net_diff=False)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u-empty",
        },
    )
    out = MergeStage().run(ctx.service.get(t.id), ctx)
    assert out.next_state is State.DONE
    assert "already satisfied" in out.note.lower()
    assert (ctx.service.workspace(t).artifacts_dir / "merge.md").exists()


def test_closed_unmerged_nonempty_branch_still_blocks(tmp_path, monkeypatch):
    """A PR closed without merge whose branch DOES carry real changes must
    still BLOCK (resumable) — never silently close real work."""
    ctx = _gh(tmp_path)
    t = _human_mr_approval(ctx)
    _seed_workspace_clone(ctx, t, net_diff=True)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u-real",
        },
    )
    out = MergeStage().run(ctx.service.get(t.id), ctx)
    assert out.next_state is State.BLOCKED
    assert "closed without merge" in out.note


def test_open_is_noop(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
        },
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL  # same state = worker no-op


def test_transient_error_is_noop(tmp_path, monkeypatch):
    ctx = _gh(tmp_path)

    def boom(self, *, source_branch):
        raise RuntimeError("api down")

    monkeypatch.setattr(github.GitHubForge, "pr_status", boom)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL  # retry next poll, not blocked


# --- mergeable flag: explicit True/None treated as mergeable (no rebase) ---


def test_open_mergeable_true_is_noop(tmp_path, monkeypatch):
    """PR open with mergeable=True → standard no-op, no rebase."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_open_mergeable_none_is_noop(tmp_path, monkeypatch):
    """mergeable=None (unchecked) → treat as mergeable, no rebase."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": None,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


# --- New: mergeable PR never enters REBASING ---


def test_mergeable_pr_never_enters_rebasing(tmp_path, monkeypatch):
    """mergeable=True/None → OUTCOME(HUMAN_MR_APPROVAL), never REBASING."""
    ctx = _gh(tmp_path)
    for mergeable in (True, None):
        monkeypatch.setattr(
            github.GitHubForge,
            "pr_status",
            lambda self, *, source_branch, m=mergeable: {
                "merged": False,
                "state": "open",
                "url": "u",
                "mergeable": m,
            },
        )
        out = MergeStage().run(_human_mr_approval(ctx), ctx)
        assert out.next_state is State.HUMAN_MR_APPROVAL
        assert "REBASING" not in str(out.next_state.value)


def test_rebasing_skips_rebase_when_pr_clean(tmp_path, monkeypatch):
    """A ticket stuck in REBASING whose PR is genuinely CLEAN (mergeable,
    up-to-date, checks passing) skips the rebase entirely (no reconcile →
    no diverged-clone BLOCK) and re-polls the gates via IMPLEMENT_COMPLETE."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": "clean",
        },
    )

    def _boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("rebase reconcile must be skipped for a clean PR")

    monkeypatch.setattr(
        merge_mod.git_ops, "reconcile_with_remote_pr", _boom, raising=False
    )

    out = MergeStage().run(_in_rebasing(ctx), ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


@pytest.mark.parametrize("mstate", ["behind", "unstable", "blocked"])
def test_rebasing_does_not_skip_when_not_clean(tmp_path, monkeypatch, mstate):
    """A mergeable-but-not-clean PR (behind main / failing CI) must NOT skip
    the rebase — that was the oscillation bug (implement_complete↔rebasing
    forever, branch never catching up to a fixed main). It proceeds to the
    conflict/rebase handler instead."""
    from robotsix_mill.stages.merge.rebase import RebaseMixin

    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": mstate,
        },
    )

    called = {}

    def _fake_handle(self, ticket, ctx, branch):
        called["handled"] = True
        return Outcome(State.REBASING)

    monkeypatch.setattr(RebaseMixin, "_handle_conflict", _fake_handle)

    out = MergeStage().run(_in_rebasing(ctx), ctx)
    assert called.get("handled") is True
    assert out.next_state is State.REBASING


# --- HUMAN_MR_APPROVAL silent fallback: conflicting → IMPLEMENT_COMPLETE ---


def test_human_mr_approval_conflicting_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """HUMAN_MR_APPROVAL + mergeable=False + update_branch fails → REBASING (autonomous rebase enabled by default)."""
    ctx = _gh(tmp_path)
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING
    assert "rebasing automatically" in out.note


def test_human_mr_approval_conflicting_kill_switch_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """HUMAN_MR_APPROVAL + mergeable=False + autonomous_rebase_enabled=False → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path, autonomous_rebase_enabled="false")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "gates no longer pass" in out.note


def test_human_mr_approval_conflicting_update_branch_succeeds_stays_in_human_mr_approval(
    tmp_path, monkeypatch
):
    """HUMAN_MR_APPROVAL + mergeable=False + update_branch succeeds → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {
            "updated": True,
            "reason": "update-branch accepted",
        },
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_human_mr_approval_ci_failing_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """HUMAN_MR_APPROVAL + mergeable=True + CI failure → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "gates no longer pass" in out.note


# --- HUMAN_MR_APPROVAL: CHANGES_REQUESTED review submitted while parked ---


def test_human_mr_approval_changes_requested_while_parked_routes_to_addressing_review(
    tmp_path, monkeypatch
):
    """A reviewer who submits CHANGES_REQUESTED *after* the ticket is parked at
    HUMAN_MR_APPROVAL must be detected on a later poll and routed to
    ADDRESSING_REVIEW — not silently ignored."""
    ctx = _gh(tmp_path, review_feedback_enabled="true")
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
        "pr_review_status",
        lambda self, *, source_branch: {
            "state": "CHANGES_REQUESTED",
            "comments": [
                {
                    "body": "data-loss bug here",
                    "path": "ci_fix.py",
                    "line": 429,
                    "review_state": "CHANGES_REQUESTED",
                }
            ],
            "files": ["ci_fix.py"],
        },
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.ADDRESSING_REVIEW
    # The review comments are persisted so the revision agent can read them.
    review_json = ctx.service.workspace(t).artifacts_dir / "review_feedback.json"
    assert review_json.exists()
    persisted = json.loads(review_json.read_text(encoding="utf-8"))
    assert persisted["state"] == "CHANGES_REQUESTED"
    assert persisted["comments"][0]["path"] == "ci_fix.py"


def test_human_mr_approval_body_only_changes_requested_is_actionable(
    tmp_path, monkeypatch
):
    """A CHANGES_REQUESTED review with an EMPTY comments list is still
    actionable: the merge stage synthesizes ONE comment from the review body
    (path='' / line=None), persists it, and routes to ADDRESSING_REVIEW —
    instead of dropping it as a no-op."""
    ctx = _gh(tmp_path, review_feedback_enabled="true")
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
        "pr_review_status",
        lambda self, *, source_branch: {
            "state": "CHANGES_REQUESTED",
            "body": "Please rework the whole approach.",
            "comments": [],
            "files": [],
        },
    )
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.ADDRESSING_REVIEW
    review_json = ctx.service.workspace(t).artifacts_dir / "review_feedback.json"
    assert review_json.exists()
    persisted = json.loads(review_json.read_text(encoding="utf-8"))
    # A comment was synthesized from the review body so the agent has
    # something to act on.
    assert len(persisted["comments"]) == 1
    synthesized = persisted["comments"][0]
    assert synthesized["body"] == "Please rework the whole approach."
    assert synthesized["path"] == ""
    assert synthesized["line"] is None


# D. Merge-stage CI branching (updated for IMPLEMENT_COMPLETE)
# ============================================================


def test_mergeable_failing_ci_falls_back_to_implement_complete(tmp_path, monkeypatch):
    """Mergeable PR + failing CI → IMPLEMENT_COMPLETE (silent fallback from HUMAN_MR_APPROVAL)."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [
                {"name": "lint", "summary": None, "text": None, "annotations": []}
            ],
        },
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_mergeable_green_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """Mergeable PR + green CI → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_mergeable_none_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """check_status returns None (no checks) → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: None,
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_mergeable_pending_ci_stays_human_mr_approval(tmp_path, monkeypatch):
    """Mergeable PR + pending CI → HUMAN_MR_APPROVAL."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_check_status_exception_is_noop(tmp_path, monkeypatch):
    """check_status raises → transient re-poll."""
    ctx = _gh(tmp_path)
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
        lambda self, *, source_branch: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_conflicting_pr_skips_check_status(tmp_path, monkeypatch):
    """Conflicting PR → update_branch attempted, then REBASING; check_status never called."""
    ctx = _gh(tmp_path)
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
    monkeypatch.setattr(
        github.GitHubForge,
        "update_branch",
        lambda self, *, source_branch: {"updated": False, "reason": "merge conflict"},
    )
    check_calls = []

    def fake_check_status(self, *, source_branch):
        check_calls.append(1)
        return {"conclusion": "success", "failing": []}

    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)

    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING
    assert check_calls == []  # never called for conflicting PR


def test_merged_pr_skips_check_status(tmp_path, monkeypatch):
    """Merged PR → DONE; check_status never called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "u",
        },
    )
    check_calls = []

    def fake_check_status(self, *, source_branch):
        check_calls.append(1)

    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.DONE
    assert check_calls == []  # never called


def test_closed_pr_skips_check_status(tmp_path, monkeypatch):
    """Closed PR → BLOCKED; check_status never called."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u",
        },
    )
    check_calls = []

    def fake_check_status(self, *, source_branch):
        check_calls.append(1)

    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)
    out = MergeStage().run(_human_mr_approval(ctx), ctx)
    assert out.next_state is State.BLOCKED
    assert check_calls == []


# ============================================================
# Review-feedback gate in the auto-merge polling paths (#...-5d9c)
# ============================================================


def test_waiting_auto_merge_changes_requested_routes_to_addressing_review(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + eligible + CI green, but a late CHANGES_REQUESTED
    review with comments → ADDRESSING_REVIEW (no merge), artifact written."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "abc1234",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: {
            "state": "CHANGES_REQUESTED",
            "comments": [{"body": "please fix", "path": "a.py", "line": 1}],
            "files": ["a.py"],
        },
    )
    merged_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: (
            merged_calls.append(1) or {"merged": True, "reason": "merged"}
        ),
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.ADDRESSING_REVIEW
    assert merged_calls == []
    artifact = ctx.service.workspace(t).artifacts_dir / "review_feedback.json"
    assert artifact.exists()
    assert json.loads(artifact.read_text(encoding="utf-8"))["comments"]


def test_waiting_auto_merge_changes_requested_empty_comments_is_noop(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + CHANGES_REQUESTED but no comments → gate does not
    fire; auto-merge proceeds to DONE."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "abc1234",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: {
            "state": "CHANGES_REQUESTED",
            "comments": [],
            "files": [],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_waiting_auto_merge_changes_requested_ignored_when_flag_disabled(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + CHANGES_REQUESTED but review_feedback_enabled=false
    → gate ignored; auto-merge proceeds to DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "abc1234",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: {
            "state": "CHANGES_REQUESTED",
            "comments": [{"body": "fix", "path": "a.py", "line": 1}],
            "files": ["a.py"],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_waiting_auto_merge_pr_review_status_raises_is_transient_noop(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + pr_review_status raises → treated as transient;
    flow continues (does not crash, does not route to ADDRESSING_REVIEW)."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "abc1234",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )

    def _boom(self, *, source_branch):
        raise RuntimeError("forge unreachable")

    monkeypatch.setattr(github.GitHubForge, "pr_review_status", _boom)
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
