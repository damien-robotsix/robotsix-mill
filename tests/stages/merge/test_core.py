import contextlib
import json
import subprocess

import pytest

from robotsix_mill.agents.rebasing import RebaseResult
from robotsix_mill.config import Settings
from robotsix_mill.core import db
from robotsix_mill.core.service import TicketService
from robotsix_mill.core.states import State
from robotsix_mill.forge import github
from robotsix_mill.stages import StageContext
from robotsix_mill.stages.base import Outcome
from robotsix_mill.stages.merge import MergeStage
from robotsix_mill.vcs.git_ops import PostPushResult, ReconcileResult


def _ctx(tmp_path, **env):
    db.reset_engine()
    env.setdefault("data_dir", str(tmp_path / "data"))
    repo_auto_merge_enabled = env.pop("repo_auto_merge_enabled", False)
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
            auto_merge_enabled=repo_auto_merge_enabled,
        ),
    )


def _human_mr_approval(ctx):
    t = ctx.service.create("x", "y")
    for st in (
        State.READY,
        State.DELIVERABLE,
        State.IMPLEMENT_COMPLETE,
        State.HUMAN_MR_APPROVAL,
    ):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _implement_complete(ctx):
    """Create a ticket in IMPLEMENT_COMPLETE state (PR open, gates not verified)."""
    t = ctx.service.create("x", "y")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        ctx.service.transition(t.id, st)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _in_rebasing(ctx):
    """Create a ticket already in REBASING state."""
    t = _implement_complete(ctx)
    ctx.service.transition(t.id, State.REBASING, note="PR conflicting")
    return ctx.service.get(t.id)


def _gh(tmp_path, **extra):
    # When auto_merge_enabled is set to "true", also opt in the repo.
    if (
        extra.get("auto_merge_enabled") == "true"
        and "repo_auto_merge_enabled" not in extra
    ):
        extra["repo_auto_merge_enabled"] = True
    return _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        **extra,
    )


def _write_review_artifact(
    ctx, ticket, *, verdict="APPROVE", eligible=True, comment="", head_sha=None
):
    """Helper: write a review.md artifact for auto-merge tests."""
    art_dir = ctx.service.workspace(ticket).artifacts_dir
    art_dir.mkdir(parents=True, exist_ok=True)
    text = f"verdict: {verdict}\nauto_merge_eligible: {str(eligible).lower()}\n"
    if head_sha is not None:
        text += f"head_sha: {head_sha}\n"
    if comment:
        text += f"comment: {comment}\n"
    (art_dir / "review.md").write_text(text, encoding="utf-8")


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
        FORGE_KIND="auto",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
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

    from robotsix_mill.vcs import git_ops

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
    import subprocess

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
# G. Git merge verification tests (new)
# ============================================================


def _git(repo, *args):
    """Run a git command in *repo*, raising on failure."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _build_repo_with_origin(tmp_path):
    """Build a work repo with an ``origin/main`` remote-tracking ref.

    Creates a bare repo used as ``origin``, a work repo with an initial
    commit on ``main`` pushed to it, and fetches so ``origin/main``
    resolves locally.  Returns the work-repo ``Path``.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial commit on main")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")
    return repo


def _waiting_auto_merge_ticket(ctx, *, sha="abc1234"):
    """Create a ticket in WAITING_AUTO_MERGE with auto-merge eligibility.

    Returns the ticket and its branch name.
    """
    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)
    branch = f"mill/{t.id}"
    return t, branch


def test_waiting_auto_merge_verify_ancestor_confirmed_goes_to_done(
    tmp_path, monkeypatch
):
    """Feature branch tip is an ancestor of origin/main → verify passes → DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    # Set up a real git repo with a feature branch merged into main.
    repo = _build_repo_with_origin(tmp_path)
    branch = "mill/test123"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("feature work\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature commit")
    _git(repo, "push", "origin", branch)
    # Merge the feature into main and push.
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", branch, "-m", "merge feature")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    feature_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Monkeypatch _workspace_repo_dir to return our real repo.
    from robotsix_mill.stages import merge as merge_mod

    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: str(repo))

    # Monkeypatch the forge: PR already merged.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "u",
            "sha": feature_tip,
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_waiting_auto_merge_verify_squash_merge_goes_to_done(tmp_path, monkeypatch):
    """Feature tip NOT an ancestor of main, but a commit on main references
    the ticket ID → squash-merge fallback → DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    # Create the ticket first so we have its id for the commit message.
    t = _human_mr_approval(ctx)

    repo = _build_repo_with_origin(tmp_path)
    branch = "mill/test123"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("feature work\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature commit")
    # Do NOT merge the branch into main (so it's not an ancestor).
    # Instead, create a squash-style commit on main that references the ticket.
    _git(repo, "checkout", "main")
    (repo / "other.txt").write_text("squash of feature\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", f"squash merge of {t.id}")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    feature_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    from robotsix_mill.stages import merge as merge_mod

    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: str(repo))

    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "u",
            "sha": feature_tip,
        },
    )

    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_waiting_auto_merge_verify_fails_goes_to_implement_complete(
    tmp_path, monkeypatch
):
    """Feature branch tip is NOT an ancestor of main AND no squash-merge
    evidence → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    repo = _build_repo_with_origin(tmp_path)
    branch = "mill/test123"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("unmerged work\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature commit")
    # Do NOT merge into main.
    _git(repo, "checkout", "main")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    feature_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    from robotsix_mill.stages import merge as merge_mod

    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: str(repo))

    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "u",
            "sha": feature_tip,
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "merge not confirmed" in out.note


def test_waiting_auto_merge_merge_pr_success_verify_fails_goes_to_implement_complete(
    tmp_path, monkeypatch
):
    """merge_pr returns {'merged': True} but the feature-tip is not an
    ancestor of main and no squash-merge evidence → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")

    repo = _build_repo_with_origin(tmp_path)
    branch = "mill/test123"
    _git(repo, "checkout", "-b", branch)
    (repo / "feature.txt").write_text("unmerged work\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature commit")
    _git(repo, "checkout", "main")
    _git(repo, "push", "origin", "main")
    _git(repo, "fetch", "origin")

    feature_tip = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    from robotsix_mill.stages import merge as merge_mod

    monkeypatch.setattr(merge_mod, "_workspace_repo_dir", lambda ctx, t: str(repo))

    # Path B: CI is green, eligibility holds, merge_pr returns success.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": feature_tip,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
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
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "merge not confirmed" in out.note


def test_cross_repo_merge_routes_to_upstream_pr(tmp_path, monkeypatch):
    """A repo with a cross_repo_target merges/polls the UPSTREAM PR:
    the forge resolved for merge_pr targets the upstream owner/repo,
    not the clone remote."""
    from robotsix_mill.config import CrossRepoTarget, RepoConfig

    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    # Replace the ctx repo_config with one carrying a cross_repo_target.
    rc = RepoConfig(
        repo_id="test-repo",
        board_id="test-board",
        langfuse_project_name="test",
        langfuse_public_key="pk-test",
        langfuse_secret_key="sk-test",
        forge_remote_url="https://github.com/fork-owner/r.git",
        auto_merge_enabled=True,
        cross_repo_target=CrossRepoTarget(
            upstream_remote_url="https://github.com/up/r.git",
            fork_remote_url="https://github.com/fork-owner/r.git",
        ),
    )
    ctx = StageContext(settings=ctx.settings, service=ctx.service, repo_config=rc)

    seen = {}
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://github.com/up/r/pull/1",
            "mergeable": True,
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )

    def fake_merge(self, *, source_branch):
        seen["owner_repo"] = self._owner_repo
        return {"merged": True, "reason": "merged"}

    monkeypatch.setattr(github.GitHubForge, "merge_pr", fake_merge)

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    # The merge targeted the UPSTREAM repo, not the fork clone remote.
    assert seen["owner_repo"] == ("up", "r")


def test_waiting_auto_merge_no_repo_proceeds_to_done(tmp_path, monkeypatch):
    """No git repo in workspace → _verify_merge_ancestor returns True
    (best-effort) → DONE as before."""
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
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


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


# Stale review artifact (head_sha mismatch) — regression tests
# for merge-gate-replays-stale-request-changes
# ============================================================


def test_auto_merge_not_blocked_by_stale_artifact_head_sha(tmp_path, monkeypatch):
    """When review.md has a different head_sha than the current PR head,
    the stale verdict must not block auto-merge (eligible=True)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "current-head-abc123",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    # Write an artifact with a DIFFERENT head_sha than the PR reports.
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="old-stale-head-xyz789",
    )

    out = MergeStage().run(t, ctx)
    # Despite auto_merge_eligible: false in artifact, the stale head_sha
    # mismatch makes it eligible → auto-merge to DONE.
    assert out.next_state is State.DONE


def test_auto_merge_not_blocked_by_current_artifact_same_head_sha(
    tmp_path,
    monkeypatch,
):
    """When review.md has the SAME head_sha as the current PR, the
    verdict is current but no longer blocks — auto-merge fires."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "same-head-123",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="same-head-123",
    )

    out = MergeStage().run(t, ctx)
    # Artifact check removed — auto-merge proceeds.
    assert out.next_state is State.DONE
    assert merge_called == [1]


def test_auto_merge_without_head_sha_in_artifact_is_backward_compat(
    tmp_path,
    monkeypatch,
):
    """When review.md has NO head_sha line (legacy pre-d42c artifact),
    the missing SHA is treated as stale — the verdict cannot be trusted
    and the PR auto-merges through to DONE instead of blocking."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "any-head",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )

    t = _human_mr_approval(ctx)
    # No head_sha line — legacy artifact, treated as stale.
    _write_review_artifact(ctx, t, verdict="REQUEST_CHANGES", eligible=False)

    out = MergeStage().run(t, ctx)
    # Legacy artifacts without head_sha are stale → auto-merge to DONE.
    assert out.next_state is State.DONE


def test_waiting_auto_merge_stale_artifact_does_not_bounce(tmp_path, monkeypatch):
    """WAITING_AUTO_MERGE poll with a stale artifact (different head_sha)
    must not bounce back to HUMAN_MR_APPROVAL — the ticket proceeds to
    auto-merge when CI is green."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "rebased-head-def456",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )

    t = _human_mr_approval(ctx)
    # Artifact from BEFORE the rebase — different head_sha.
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="pre-rebase-head-abc111",
    )
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    # Should not bounce — stale artifact is ignored, proceeds to auto-merge.
    assert out.next_state is State.DONE


def test_waiting_auto_merge_legacy_artifact_no_head_sha_does_not_bounce(
    tmp_path,
    monkeypatch,
):
    """WAITING_AUTO_MERGE poll with a legacy artifact that has NO head_sha
    line (pre-d42c cache) must not bounce back to HUMAN_MR_APPROVAL —
    the missing SHA means the verdict is untrusted and the ticket
    proceeds to auto-merge when CI is green."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "post-push-head-456",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )

    t = _human_mr_approval(ctx)
    # Legacy artifact — no head_sha line at all.
    _write_review_artifact(ctx, t, verdict="REQUEST_CHANGES", eligible=False)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    # Should not bounce — legacy artifact treated as stale, proceeds to auto-merge.
    assert out.next_state is State.DONE


def test_waiting_auto_merge_current_artifact_no_longer_bounces(
    tmp_path,
    monkeypatch,
):
    """WAITING_AUTO_MERGE poll with a CURRENT artifact (same head_sha)
    that is not eligible — artifact check removed, stays WAITING_AUTO_MERGE
    when CI is pending."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "current-head-999",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "pending", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="current-head-999",
    )
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    # Artifact check removed — stays in WAITING_AUTO_MERGE (CI pending).
    assert out.next_state is State.WAITING_AUTO_MERGE


def test_stale_artifact_no_longer_blocks_auto_merge(tmp_path, monkeypatch):
    """When a stale review artifact exists (head_sha mismatch), auto-merge
    still fires — the artifact check has been removed entirely."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "sha": "rebased-head-abc",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success", "failing": []},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_files",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "get_authenticated_user_login",
        lambda self: "mill-bot",
    )

    t = _human_mr_approval(ctx)
    ws = ctx.service.workspace(t)

    # Pre-populate a review stage cache entry — simulates a cached
    # REQUEST_CHANGES outcome from before the rebase.
    import json

    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    cache_path = ws.artifacts_dir / "stage_cache.json"
    cache_path.write_text(
        json.dumps(
            {"review": {"input_hash": "old-hash", "next_state": "ready", "note": ""}}
        ),
        encoding="utf-8",
    )

    # Write a review artifact with a DIFFERENT head_sha (stale).
    _write_review_artifact(
        ctx,
        t,
        verdict="REQUEST_CHANGES",
        eligible=False,
        head_sha="old-stale-sha-xyz",
    )

    out = MergeStage().run(t, ctx)
    # Artifact check removed — auto-merge to DONE.
    assert out.next_state is State.DONE


def test_stale_changes_requested_dismissed_regardless_of_feedback_flag(
    tmp_path, monkeypatch
):
    """Regression: a stale CHANGES_REQUESTED forge review against an old
    commit is dismissed on the forge and does NOT prevent auto-merge,
    even when review_feedback_enabled is False.

    Scenario (from ticket b3fb / PR #2446):
    - MR is approved (review.md with auto_merge_eligible: true)
    - A new commit is pushed, superseding an earlier CHANGES_REQUESTED review
    - CI is green on the new head
    - The merge gate must not bounce back to human_mr_approval

    This test runs with review_feedback_enabled=False to prove the
    stale-review dismissal path works independently of the feedback gate.
    """
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    # review_feedback_enabled is NOT set — defaults to False.

    dismissed_ids: list[int] = []

    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://gh/o/r/pull/1",
            "mergeable": True,
            "sha": "new-head-abc222",
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
            "commit_id": "old-head-abc111",
            "review_id": 42,
        },
    )

    def _dismiss(self, *, source_branch, review_id):
        dismissed_ids.append(review_id)
        return True

    monkeypatch.setattr(github.GitHubForge, "dismiss_review", _dismiss)
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
    # Must auto-merge → DONE; must NOT bounce back to human_mr_approval.
    assert out.next_state is State.DONE
    # The stale review must have been dismissed.
    assert dismissed_ids == [42]


def test_stale_changes_requested_dismissed_with_feedback_enabled(tmp_path, monkeypatch):
    """Same scenario as above but with review_feedback_enabled=True.
    The stale review is still dismissed and auto-merge proceeds."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )

    dismissed_ids: list[int] = []

    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://gh/o/r/pull/1",
            "mergeable": True,
            "sha": "new-head-abc222",
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
            "commit_id": "old-head-abc111",
            "review_id": 42,
        },
    )

    def _dismiss(self, *, source_branch, review_id):
        dismissed_ids.append(review_id)
        return True

    monkeypatch.setattr(github.GitHubForge, "dismiss_review", _dismiss)
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
    # Must auto-merge → DONE.
    assert out.next_state is State.DONE
    # The stale review must have been dismissed.
    assert dismissed_ids == [42]


# ============================================================
# Green-but-unpromotable ceiling (renamed required check)
# ============================================================


def _green_unpromotable_ctx(tmp_path, monkeypatch, **extra):
    """Forge stubs for "every reported check passed, forge still won't promote"."""
    ctx = _gh(tmp_path, **extra)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://forge/pr/1",
            "mergeable": True,
            # Protection is waiting on a context nothing reports, so the PR
            # never leaves "blocked" no matter how long the mill polls.
            "mergeable_state": "blocked",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
            "jobs": [{"name": "Lint", "conclusion": "success"}],
        },
    )
    return ctx


def test_green_unpromotable_re_polls_below_the_ceiling(tmp_path, monkeypatch):
    """Settling is normal: the first polls must keep waiting, not block."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.green_unpromotable_max_polls - 1):
        out = stage.run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE


def test_green_unpromotable_blocks_at_the_ceiling_naming_the_missing_context(
    tmp_path, monkeypatch
):
    """Green CI + a permanently unpromotable PR escalates with an actionable note."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(
        github.GitHubForge,
        "required_status_contexts",
        lambda self, *, target_branch: ["lint", "typecheck"],
    )
    t = _implement_complete(ctx)
    stage = MergeStage()
    outs = [stage.run(t, ctx) for _ in range(ctx.settings.green_unpromotable_max_polls)]

    assert [o.next_state for o in outs[:-1]] == [State.IMPLEMENT_COMPLETE] * (
        len(outs) - 1
    )
    final = outs[-1]
    assert final.next_state is State.BLOCKED
    # The two required contexts the PR never reports must be named; the one
    # it does report ("Lint") must not be.
    assert "'lint'" in final.note
    assert "'typecheck'" in final.note
    assert "https://forge/pr/1" in final.note


def test_green_unpromotable_falls_back_when_protection_is_unreadable(
    tmp_path, monkeypatch
):
    """No protection data (403/unprotected) still blocks, with a vaguer note."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch)
    monkeypatch.setattr(
        github.GitHubForge,
        "required_status_contexts",
        lambda self, *, target_branch: [],
    )
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.green_unpromotable_max_polls - 1):
        stage.run(t, ctx)
    final = stage.run(t, ctx)
    assert final.next_state is State.BLOCKED
    assert "mergeable_state='blocked'" in final.note


def test_green_unpromotable_counter_resets_while_a_check_is_pending(
    tmp_path, monkeypatch
):
    """A pending check is genuine settling and must not consume the budget."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(ctx.settings.green_unpromotable_max_polls - 1):
        assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE

    # One poll where CI is still running clears the accumulated count...
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "pending",
            "failing": [],
            "pending": ["Test"],
            "jobs": [],
        },
    )
    assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE

    # ...so the next green-but-stuck poll starts the budget over.
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch, require_checks=False: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
            "jobs": [{"name": "Lint", "conclusion": "success"}],
        },
    )
    assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE


def test_green_unpromotable_guard_disabled_by_zero(tmp_path, monkeypatch):
    """green_unpromotable_max_polls=0 restores the old unbounded re-poll."""
    ctx = _green_unpromotable_ctx(tmp_path, monkeypatch, green_unpromotable_max_polls=0)
    t = _implement_complete(ctx)
    stage = MergeStage()
    for _ in range(12):
        assert stage.run(t, ctx).next_state is State.IMPLEMENT_COMPLETE
