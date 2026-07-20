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
from robotsix_mill.stages.merge import MergeStage, _read_counter, _write_counter
from robotsix_mill.vcs.git_ops import PostPushResult, ReconcileResult


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
    return _ctx(
        tmp_path,
        FORGE_KIND="github",
        FORGE_TOKEN="t",
        FORGE_REMOTE_URL="https://github.com/o/r.git",
        **extra,
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
    assert out.next_state is State.BLOCKED and "forge not configured" in out.note


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
    assert out.next_state is State.DONE and "pull/3" in out.note
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
    """HUMAN_MR_APPROVAL + mergeable=False → IMPLEMENT_COMPLETE (silent fallback)."""
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
    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "gates no longer pass" in out.note


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


# --- REBASING path: clean rebase → IMPLEMENT_COMPLETE ---


def test_rebasing_clean_rebase_returns_to_implement_complete(tmp_path, monkeypatch):
    """Ticket in REBASING → rebase agent succeeds → post-check passes → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, target=target, remote_url=remote_url)
        return PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    # Post-rebase routing checks whether a PR exists; mock so the
    # forge reports a PR → route stays IMPLEMENT_COMPLETE (regression).
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
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert post_check_calls["branch"] == f"mill/{t.id}"


def test_rebase_clears_stale_review_artifact_and_cache(tmp_path, monkeypatch):
    """After a successful rebase, the review.md artifact and the review
    stage-outcome cache must be cleared so a subsequent review pass
    evaluates the current diff rather than replaying a stale verdict."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
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
    ws = ctx.service.workspace(t)
    repo_dir = ws.dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Pre-populate a review.md artifact and a stage cache entry to
    # simulate a pre-rebase REQUEST_CHANGES verdict.
    ws.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (ws.artifacts_dir / "review.md").write_text(
        "verdict: REQUEST_CHANGES\n"
        "auto_merge_eligible: false\n"
        "head_sha: old-stale-sha\n"
        "comment: build artifacts in diff\n",
        encoding="utf-8",
    )
    # Write a stage_cache.json with a "review" entry.
    import json

    cache_path = ws.artifacts_dir / "stage_cache.json"
    cache_path.write_text(
        json.dumps(
            {"review": {"input_hash": "abc123", "next_state": "ready", "note": ""}}
        ),
        encoding="utf-8",
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE

    # After rebase, review.md must be removed.
    assert not (ws.artifacts_dir / "review.md").exists(), (
        "review.md must be deleted after successful rebase"
    )

    # After rebase, the review stage cache entry must be cleared.
    cache = (
        json.loads(cache_path.read_text(encoding="utf-8"))
        if cache_path.exists()
        else {}
    )
    assert "review" not in cache, (
        "review stage cache entry must be removed after successful rebase"
    )


def test_rebasing_push_targets_per_repo_remote(tmp_path, monkeypatch):
    """Regression: the post-rebase force-push must target the ticket's
    *per-repo* remote, not the global FORGE_REMOTE_URL.

    A ticket on a non-mill board whose rebased commit was pushed to the
    global (mill) remote left the real PR branch untouched → GitHub kept
    reporting the PR conflicting → endless REBASING → BLOCKED.
    """
    from robotsix_mill.config import RepoConfig

    base = _gh(tmp_path)  # global FORGE_REMOTE_URL = https://github.com/o/r.git
    per_repo_url = "https://github.com/o/other-repo.git"
    ctx = StageContext(
        settings=base.settings,
        service=base.service,
        repo_config=RepoConfig(
            repo_id="other-repo",
            board_id="test-board",
            langfuse_project_name="test",
            langfuse_public_key="pk-test",
            langfuse_secret_key="sk-test",
            forge_remote_url=per_repo_url,
        ),
    )

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None: (
            RebaseResult(status="DONE", summary="ok")
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch", lambda *a, **k: None
    )

    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check", fake_post_check
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
    assert out.next_state is State.IMPLEMENT_COMPLETE
    # The push must go to the per-repo remote, not the global one.
    assert post_check_calls["remote_url"] == per_repo_url


def test_rebasing_success_no_pr_routes_to_ready(tmp_path, monkeypatch):
    """Rebase agent succeeds, post-check passes, but no PR exists for the
    branch → route to READY so the ticket re-enters implement."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, target=target, remote_url=remote_url)
        return PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    # pr_status returns None → no PR exists → route to READY.
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.READY
    assert post_check_calls["branch"] == f"mill/{t.id}"


def test_rebasing_noop_skips_force_push(tmp_path, monkeypatch):
    """Rebase agent succeeds; post_push_check is always called to verify
    the agent-driven push actually landed. The old deterministic no-op
    skip (local==remote → skip push) is gone — the agent pushes, the
    stage only verifies."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.head_sha",
        lambda repo: sha,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.remote_branch_sha",
        lambda repo, branch: sha,
    )
    post_check_calls = []
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **kw: post_check_calls.append(1) or PostPushResult.PASS,
    )
    # Need a PR status so the stage routes to IMPLEMENT_COMPLETE, not READY.
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
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert post_check_calls == [1]  # post_push_check IS called


def test_rebasing_noop_blocks_after_max_attempts(tmp_path, monkeypatch):
    """A rebase that never resolves the conflict is bounded: once
    the attempt budget is spent the ticket goes BLOCKED (once), instead
    of ping-ponging forever. The post-check still passes."""
    ctx = _gh(tmp_path, rebase_max_attempts="2")
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **kw: PostPushResult.PASS,
    )
    # PR still reports conflicting (not mergeable) so attempts are counted
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

    # attempt 1 → IMPLEMENT_COMPLETE (re-poll), attempt 2 (== max) → BLOCKED
    o1 = MergeStage().run(t, ctx)
    assert o1.next_state is State.IMPLEMENT_COMPLETE
    o2 = MergeStage().run(ctx.service.get(t.id), ctx)
    assert o2.next_state is State.BLOCKED


# --- REBASING: retry stays REBASING ---


def test_rebasing_retry_stays_rebasing(tmp_path, monkeypatch):
    """REBASING, rebase fails, attempt < max → Outcome(REBASING)."""
    ctx = _gh(tmp_path, rebase_max_attempts="3")
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

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.REBASING  # retry, not IMPLEMENT_COMPLETE

    counter_path = ctx.service.workspace(t).artifacts_dir / "rebase_attempts.txt"
    assert _read_counter(counter_path) == 1


# --- REBASING: exhausted → BLOCKED ---


def test_rebasing_exhausted_blocks(tmp_path, monkeypatch):
    """REBASING, rebase fails, attempt == max → Outcome(BLOCKED)."""
    ctx = _gh(tmp_path, rebase_max_attempts="1")

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_called = []

    def fake_push(*a, **k):
        push_called.append(1)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "rebase failed after 1 attempt" in out.note
    assert push_called == []  # never force-pushed on failure


def test_rebase_failure_note_surfaces_conflicts_and_agent_detail(tmp_path, monkeypatch):
    """The BLOCKED note names the conflicting file(s) and the rebase agent's
    own explanation instead of a generic 'manual conflict resolution
    required' (better operator feedback)."""
    ctx = _gh(tmp_path, rebase_max_attempts="1")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(
            status="FAILED",
            summary="both sides rewrote tests/test_reconcile.py; a human must "
            "decide which assertions to keep",
        ),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.conflicted_files",
        lambda repo: ["tests/test_reconcile.py"],
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "tests/test_reconcile.py" in out.note
    assert "both sides rewrote" in out.note
    assert "rebase failed after 1 attempt" in out.note


# --- Full cycle: IMPLEMENT_COMPLETE → REBASING → IMPLEMENT_COMPLETE ---


def test_implement_complete_to_rebasing_and_back(tmp_path, monkeypatch):
    """Full cycle: IMPLEMENT_COMPLETE + mergeable=False → REBASING → then rebase success → IMPLEMENT_COMPLETE."""
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
    calls = {}

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        calls.update(repo_dir=repo_dir, branch=branch, target=target)
        return RebaseResult(status="DONE", summary="ok")  # success

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    post_check_calls = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        post_check_calls.update(branch=branch, remote_url=remote_url)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    t = _implement_complete(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Step 1: IMPLEMENT_COMPLETE + conflicting → REBASING.
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert calls == {}  # agent not called yet

    # Actually transition the ticket to REBASING.
    ctx.service.transition(t.id, State.REBASING, note="conflicting")
    t = ctx.service.get(t.id)

    # Switch pr_status to report a PR exists (mergeable=True) so the
    # post-rebase routing stays IMPLEMENT_COMPLETE.
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

    # Step 2: REBASING → rebase agent runs, succeeds → IMPLEMENT_COMPLETE.
    out2 = MergeStage().run(t, ctx)
    assert calls["branch"] == f"mill/{t.id}"
    assert calls["target"] == "main"
    assert str(repo_dir) in calls["repo_dir"]
    assert post_check_calls["branch"] == f"mill/{t.id}"
    assert out2.next_state is State.IMPLEMENT_COMPLETE


def test_rebasing_no_workspace_clone_blocks(tmp_path, monkeypatch):
    """If the workspace clone is missing in REBASING, cannot rebase → BLOCKED."""
    ctx = _gh(tmp_path)
    # No repo dir created — workspace is empty.
    t = _in_rebasing(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "workspace clone is missing" in out.note


def test_rebase_failure_exhausts_attempts_then_blocks(tmp_path, monkeypatch):
    """Agent returns False for every attempt → BLOCKED after max (through REBASING)."""
    ctx = _gh(tmp_path, rebase_max_attempts="2")

    agent_calls = []

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        agent_calls.append(1)
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    # Attempt 1: agent returns False → stays REBASING (retry next poll)
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert len(agent_calls) == 1

    # Attempt 2: agent returns False again → exhausted → BLOCKED
    out2 = MergeStage().run(t, ctx)
    assert out2.next_state is State.BLOCKED
    assert "rebase failed after 2 attempt" in out2.note
    assert len(agent_calls) == 2


def test_rebase_agent_crash_is_treated_as_failure(tmp_path, monkeypatch):
    """If the agent raises, treat as False — failure path (through REBASING)."""
    ctx = _gh(tmp_path, rebase_max_attempts="1")

    def boom(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        raise RuntimeError("LLM timeout")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        boom,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "rebase failed after 1 attempt" in out.note


def test_no_force_push_on_rebase_failure(tmp_path, monkeypatch):
    """When agent returns False, no force-push is made (through REBASING)."""
    ctx = _gh(tmp_path, rebase_max_attempts="1")

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_called = []

    def fake_push(*a, **k):
        push_called.append(1)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_push,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)
    assert push_called == []  # never called


def test_push_failure_after_rebase_success_blocks(tmp_path, monkeypatch):
    """Rebase succeeds but post_push_check reports NOT_LANDED → BLOCKED."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda *a, **kw: PostPushResult.NOT_LANDED,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "push did not land" in out.note


def test_rebase_counter_resets_only_when_pr_becomes_mergeable(tmp_path, monkeypatch):
    """A push is NOT proof the conflict is resolved (git rebase rewrites
    SHAs every run). The attempt counter must persist across rebase+push
    cycles and only reset to 0 when the IMPLEMENT_COMPLETE poll sees a
    mergeable PR — otherwise the loop is unbounded."""
    ctx = _gh(tmp_path, rebase_max_attempts="3")

    call_count = [0]

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        call_count[0] += 1
        # First call fails, second succeeds.
        if call_count[0] == 2:
            return RebaseResult(status="DONE", summary="ok")
        return RebaseResult(status="FAILED", summary="nope")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    # The PR is conflicting (mergeable=False) WHILE rebasing, then becomes
    # mergeable once the conflict is truly resolved. A mutable holder lets
    # the final IMPLEMENT_COMPLETE poll see mergeable=True.
    mergeable = [False]
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": mergeable[0],
        },
    )

    def fake_post_check(repo, branch, target, remote_url, token):
        return PostPushResult.PASS

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    counter_path = ctx.service.workspace(t).artifacts_dir / "rebase_attempts.txt"

    # Attempt 1 fails → counter=1, stays REBASING
    out1 = MergeStage().run(t, ctx)
    assert out1.next_state is State.REBASING
    assert _read_counter(counter_path) == 1

    # Attempt 2 succeeds+pushes → back to IMPLEMENT_COMPLETE, but counter is
    # PERSISTED (==2), NOT reset — a push doesn't prove resolution.
    out2 = MergeStage().run(t, ctx)
    assert out2.next_state is State.IMPLEMENT_COMPLETE
    assert _read_counter(counter_path) == 2

    # Now the IMPLEMENT_COMPLETE poll sees a genuinely mergeable + CI green PR
    # → the conflict is really gone → counter resets to 0 AND ticket
    # promotes to HUMAN_MR_APPROVAL (gates passed).
    mergeable[0] = True
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {"conclusion": "success"},
    )
    ctx.service.transition(t.id, State.IMPLEMENT_COMPLETE, note="rebased")
    out3 = MergeStage().run(ctx.service.get(t.id), ctx)
    assert out3.next_state is State.HUMAN_MR_APPROVAL  # promoted
    assert _read_counter(counter_path) == 0  # counter reset during poll


def test_force_push_refspec_is_ticket_branch_only(tmp_path, monkeypatch):
    """The force-push must reference only the ticket's own branch."""
    ctx = _gh(tmp_path)

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
    ):
        return RebaseResult(status="DONE", summary="ok")

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        fake_rebase,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )

    push_args = {}

    def fake_post_check(repo, branch, target, remote_url, token):
        push_args.update(branch=branch, remote_url=remote_url, token=token)

    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        fake_post_check,
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)

    # Branch pushed is the ticket's branch, not the target.
    assert push_args["branch"] == f"mill/{t.id}"
    assert push_args["branch"] != "main"  # never push target branch


def test_counter_read_write(tmp_path):
    """Unit tests for the attempt counter helpers."""
    p = tmp_path / "counter.txt"
    assert _read_counter(p) == 0  # missing file
    p.write_text("garbage")
    assert _read_counter(p) == 0  # unparseable
    _write_counter(p, 5)
    assert _read_counter(p) == 5
    _write_counter(p, 0)
    assert _read_counter(p) == 0


def test_rebase_force_push_uses_minted_token_not_raw_forge_token(tmp_path, monkeypatch):
    """Regression: the post-rebase force-push must use github_token()
    (the minted App/PAT token) — not the raw s.forge_token, which is
    empty under GitHub App auth -> unauthenticated push -> git exit 128
    -> ticket BLOCKED. The rebase+push moved to the REBASING-state path
    (#26), so drive the ticket through REBASING here."""
    ctx = _gh(tmp_path)  # FORGE_TOKEN="t" (raw); minted token differs
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.run_rebase_agent",
        lambda **k: RebaseResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.fetch",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.github_token",
        lambda s, repo_config=None: "MINTED-APP-TOK",
    )
    seen = {}
    monkeypatch.setattr(
        "robotsix_mill.stages.merge.git_ops.post_push_check",
        lambda repo, branch, target, remote_url, token: seen.update(token=token),
    )

    t = _in_rebasing(ctx)
    repo_dir = ctx.service.workspace(t).dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / ".git").mkdir(exist_ok=True)

    MergeStage().run(t, ctx)

    assert seen.get("token") == "MINTED-APP-TOK"  # not the raw "t"


# ============================================================
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
    """Conflicting PR → silent fallback to IMPLEMENT_COMPLETE; check_status never called."""
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
    check_calls = []

    def fake_check_status(self, *, source_branch):
        check_calls.append(1)
        return {"conclusion": "success", "failing": []}

    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)

    t = _human_mr_approval(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
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
        return None

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
        return None

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
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
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
        *, settings, repo_dir, branch, target, memory="", remote_url=None, token=None
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
# E. Auto-merge gate (new)
# ============================================================


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


def test_auto_merge_fires_when_all_conditions_met(tmp_path, monkeypatch):
    """Mergeable + CI success + auto_merge_enabled + review_enabled +
    artifact auto_merge_eligible: true + merge_pr returns merged → DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://gh/o/r/pull/1",
            "mergeable": True,
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

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "auto-merged" in out.note


def test_auto_merge_skipped_when_flag_disabled(tmp_path, monkeypatch):
    """auto_merge_enabled=False → HUMAN_MR_APPROVAL (standard no-op)."""
    ctx = _gh(tmp_path, auto_merge_enabled="false", review_enabled="true")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert merge_called == []


def test_auto_merge_skipped_when_review_disabled(tmp_path, monkeypatch):
    """review_enabled=False → HUMAN_MR_APPROVAL even when auto_merge_enabled=True
    and artifact says eligible."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="false")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert merge_called == []


def test_auto_merge_skipped_when_no_review_artifact(tmp_path, monkeypatch):
    """No review artifact — auto-merge still fires (artifact gate removed)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    # NO review artifact written

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert merge_called == [1]


def test_auto_merge_fires_regardless_of_artifact_verdict(tmp_path, monkeypatch):
    """Artifact auto_merge_eligible: false no longer blocks — the upstream
    human_mr_approval gate is the authoritative review decision."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t, eligible=False)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert merge_called == [1]


def test_auto_merge_skipped_when_ci_pending(tmp_path, monkeypatch):
    """CI conclusion is 'pending', not 'success' → WAITING_AUTO_MERGE
    (auto-merge gate entered, waiting for CI to go green)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE
    assert merge_called == []  # merge_pr not called for pending CI


def test_auto_merge_skipped_when_ci_failure(tmp_path, monkeypatch):
    """CI conclusion is 'failure' → IMPLEMENT_COMPLETE (silent fallback)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert merge_called == []


def test_auto_merge_skipped_when_not_mergeable(tmp_path, monkeypatch):
    """mergeable=False → IMPLEMENT_COMPLETE (silent fallback)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert merge_called == []


def test_merge_pr_failure_stays_human_mr_approval(tmp_path, monkeypatch):
    """merge_pr returns {'merged': False} → HUMAN_MR_APPROVAL (not BLOCKED)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {
            "merged": False,
            "reason": "branch protection",
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL


def test_auto_merge_writes_merge_artifact(tmp_path, monkeypatch):
    """On success, merge.md is written with the PR URL."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "https://gh/o/r/pull/42",
            "mergeable": True,
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

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    merge_artifact = ctx.service.workspace(t).artifacts_dir / "merge.md"
    assert merge_artifact.exists()
    content = merge_artifact.read_text(encoding="utf-8")
    assert "auto-merged: https://gh/o/r/pull/42" in content


# ============================================================
# F. WAITING_AUTO_MERGE — updated for IMPLEMENT_COMPLETE fallback
# ============================================================


def test_eligible_pending_ci_goes_to_waiting_auto_merge(tmp_path, monkeypatch):
    """Eligible + CI pending → WAITING_AUTO_MERGE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    merge_called = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: merge_called.append(1) or {},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE
    assert merge_called == []  # merge_pr never called


def test_eligible_success_auto_merges_to_done(tmp_path, monkeypatch):
    """Eligible + CI success → DONE (already covered, ensure it still passes)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True, "reason": "merged"},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_eligible_forge_merge_failed_stays_human_mr_approval_with_comment(
    tmp_path, monkeypatch
):
    """Eligible + CI success + forge rejects → HUMAN_MR_APPROVAL + comment."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {
            "merged": False,
            "reason": "branch protection",
        },
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "forge merge failed: branch protection" in (merge_events[0].note or "")


def test_not_eligible_disabled_flag_stays_human_mr_approval_with_comment(
    tmp_path, monkeypatch
):
    """auto_merge_enabled=false → HUMAN_MR_APPROVAL + comment."""
    ctx = _gh(tmp_path, auto_merge_enabled="false", review_enabled="true")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "auto-merge disabled in config" in (merge_events[0].note or "")


def test_not_eligible_review_disabled_stays_human_mr_approval_with_comment(
    tmp_path, monkeypatch
):
    """review_enabled=false → HUMAN_MR_APPROVAL + comment."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="false")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL

    merge_events = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events) == 1

    assert "review gate disabled" in (merge_events[0].note or "")


def test_no_review_artifact_auto_merges_when_eligible(tmp_path, monkeypatch):
    """No review artifact — auto-merge still fires (artifact check removed)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True},
    )

    t = _human_mr_approval(ctx)
    # NO review artifact

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_not_eligible_flagged_false_auto_merges_anyway(tmp_path, monkeypatch):
    """auto_merge_eligible: false no longer blocks — auto-merge fires."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t, eligible=False, comment="risky migration")

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_not_eligible_no_comment_line_auto_merges_anyway(tmp_path, monkeypatch):
    """Review artifact with eligible=False and no comment — auto-merge
    still fires (artifact check removed)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: {"merged": True},
    )

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t, eligible=False)  # no comment

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_comment_dedup_same_reason_no_duplicate(tmp_path, monkeypatch):
    """Two polls with the same reason → exactly 1 comment."""
    ctx = _gh(tmp_path, auto_merge_enabled="false", review_enabled="true")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    # First poll → writes comment.
    MergeStage().run(t, ctx)
    merge_events_ = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events_) == 1

    # Second poll — same conditions, same reason → no new comment.
    MergeStage().run(t, ctx)
    merge_events_ = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events_) == 1


def test_comment_dedup_different_reason_new_comment(tmp_path, monkeypatch):
    """Reason changes → new comment fires."""
    ctx = _gh(tmp_path, auto_merge_enabled="false", review_enabled="true")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)

    # First poll → disabled flag comment.
    MergeStage().run(t, ctx)
    merge_events_ = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events_) == 1

    # Hack: change the stored reason to simulate a prior different
    # reason (e.g., was CI pending, now CI succeeded but still not
    # eligible). Then re-run — the new reason text differs.
    reason_path = ctx.service.workspace(t).artifacts_dir / "merge_reason.txt"
    reason_path.write_text("old different reason", encoding="utf-8")

    MergeStage().run(t, ctx)
    merge_events_ = [
        e for e in ctx.service.history(t.id) if (e.note or "").startswith("merge:")
    ]

    assert len(merge_events_) == 2


def test_waiting_auto_merge_becomes_implement_complete_on_ci_failure(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE poll where CI now fails → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    # Transition to WAITING_AUTO_MERGE manually (simulate previous poll).
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE


def test_waiting_auto_merge_stays_waiting_when_ci_pending(tmp_path, monkeypatch):
    """WAITING_AUTO_MERGE poll with CI still pending → stays WAITING_AUTO_MERGE
    (artifact verdict no longer affects eligibility)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    t = _human_mr_approval(ctx)
    # Write the artifact as eligible so the WAITING_AUTO_MERGE
    # transition is plausible.
    _write_review_artifact(ctx, t, eligible=True)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    # Now change the artifact to not eligible — this no longer matters.
    _write_review_artifact(ctx, t, eligible=False)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.WAITING_AUTO_MERGE


def test_waiting_auto_merge_to_done_on_ci_success(tmp_path, monkeypatch):
    """WAITING_AUTO_MERGE poll where CI is now green → DONE."""
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
# WAITING_AUTO_MERGE → IMPLEMENT_COMPLETE on conflict
# ============================================================


def test_waiting_auto_merge_conflicting_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + mergeable=False → IMPLEMENT_COMPLETE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
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

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "gates no longer pass" in out.note


def _green_behind_forge(monkeypatch):
    """Patch the forge: PR open+mergeable but behind target, CI green."""
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "open",
            "url": "u",
            "mergeable": True,
            "mergeable_state": "behind",
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "success",
            "failing": [],
            "pending": [],
        },
    )


def test_waiting_auto_merge_green_behind_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """WAITING_AUTO_MERGE + green CI + branch behind target →
    IMPLEMENT_COMPLETE, so the gate check dispatches the rebase agent.
    Without this the ticket waits forever — a behind PR never merges
    under a strict up-to-date branch policy."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    _green_behind_forge(monkeypatch)

    t = _human_mr_approval(ctx)
    _write_review_artifact(ctx, t)
    ctx.service.transition(t.id, State.WAITING_AUTO_MERGE, note="CI pending")
    t = ctx.service.get(t.id)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "behind" in (out.note or "")


def test_human_mr_approval_green_behind_falls_back_to_implement_complete(
    tmp_path, monkeypatch
):
    """HUMAN_MR_APPROVAL + green CI + branch behind target →
    silent fallback to IMPLEMENT_COMPLETE (mirrors the conflict fallback)
    so the rebase agent catches the branch up."""
    ctx = _gh(tmp_path)
    _green_behind_forge(monkeypatch)

    t = _human_mr_approval(ctx)

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert "behind" in (out.note or "")


# ============================================================
# Multi-repo PR aggregation
# ============================================================


def _install_multirepo_registry(entries: list[tuple[str, str]]) -> None:
    """Populate the global ``_repos_config`` for multi-repo tests."""
    from robotsix_mill.config import RepoConfig, ReposRegistry, _reset_repos_config
    import robotsix_mill.config as _cfg

    _reset_repos_config()
    _cfg._repos_config = ReposRegistry(
        repos={
            rid: RepoConfig(
                repo_id=rid,
                board_id="meta",
                langfuse_project_name=f"p-{rid}",
                langfuse_public_key=f"pk-{rid}",
                langfuse_secret_key=f"sk-{rid}",
                forge_remote_url=url,
            )
            for rid, url in entries
        }
    )


@pytest.fixture(autouse=True)
def _reset_multirepo_registry_after_each_test():
    """Drop any test-installed ReposRegistry so module-global state
    never leaks between tests."""
    yield
    from robotsix_mill.config import _reset_repos_config

    _reset_repos_config()


def _write_pr_urls(ctx, ticket, entries: list[dict]) -> None:
    """Write a ``pr_urls.json`` manifest into the ticket's workspace."""
    ws = ctx.service.workspace(ticket)
    (ws.artifacts_dir / "pr_urls.json").write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )


def _make_meta_ticket(ctx, *, state=State.IMPLEMENT_COMPLETE):
    """Create a ticket and transition it to *state* (default
    ``IMPLEMENT_COMPLETE`` — the multi-repo aggregator's first
    polling state)."""
    t = ctx.service.create("Cross-repo feature", "do x in many repos")
    for st in (State.READY, State.DELIVERABLE, State.IMPLEMENT_COMPLETE):
        ctx.service.transition(t.id, st)
    if state is not State.IMPLEMENT_COMPLETE:
        ctx.service.transition(t.id, state)
    ctx.service.set_branch(t.id, f"mill/{t.id}")
    return ctx.service.get(t.id)


def _route_by_remote(
    monkeypatch,
    *,
    pr_responses: dict,
    ci_responses: dict | None = None,
    pr_by_url_responses: dict | None = None,
):
    """Monkeypatch the GitHub forge's ``pr_status`` + ``check_status``
    (and optionally ``pr_status_by_url``) so each call returns the
    response keyed by ``self._remote_url``.

    *pr_responses* / *ci_responses* / *pr_by_url_responses* are
    ``{remote_url: response | Exception}``.  A value can be a callable
    taking no args, an Exception (raised), or a plain dict / None
    (returned).
    """
    seen_pr: list[dict] = []
    seen_ci: list[dict] = []

    def fake_pr_status(self, *, source_branch):
        seen_pr.append({"remote": self._remote_url, "branch": source_branch})
        resp = pr_responses.get(self._remote_url)
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(github.GitHubForge, "pr_status", fake_pr_status)

    if pr_by_url_responses is not None:

        def fake_pr_status_by_url(self, *, url):
            resp = pr_by_url_responses.get(self._remote_url)
            if callable(resp) and not isinstance(resp, Exception):
                resp = resp()
            if isinstance(resp, Exception):
                raise resp
            return resp

        monkeypatch.setattr(
            github.GitHubForge, "pr_status_by_url", fake_pr_status_by_url
        )

    if ci_responses is not None:

        def fake_check_status(self, *, source_branch):
            seen_ci.append({"remote": self._remote_url, "branch": source_branch})
            resp = ci_responses.get(self._remote_url)
            if isinstance(resp, Exception):
                raise resp
            return resp

        monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)

    return seen_pr, seen_ci


def test_multi_repo_all_prs_merged_transitions_to_done(tmp_path, monkeypatch):
    """All N per-repo PRs merged → DONE; ``merge.md`` is the multi-line
    multi-repo header with one entry per repo."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/b/pull/2",
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "https://github.com/o/a/pull/1" in out.note
    assert "https://github.com/o/b/pull/2" in out.note

    merge_md = (ctx.service.workspace(t).artifacts_dir / "merge.md").read_text(
        encoding="utf-8"
    )
    assert merge_md.startswith("# Merge (multi-repo)")
    assert "repo-a" in merge_md
    assert "repo-b" in merge_md
    assert "https://github.com/o/a/pull/1" in merge_md
    assert "https://github.com/o/b/pull/2" in merge_md


def test_multi_repo_all_prs_merged_via_url_fallback_transitions_to_done(
    tmp_path, monkeypatch
):
    """Head branches auto-deleted on merge → branch-keyed ``pr_status``
    returns ``None`` for every repo, but the URL-keyed fallback reports
    each PR merged → DONE with the recorded URLs in ``out.note`` and
    ``merge.md``."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        # Branch-keyed lookup is empty (head branch auto-deleted).
        pr_responses={remote_a: None, remote_b: None},
        # URL-keyed fallback resolves the merged PRs.
        pr_by_url_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/b/pull/2",
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert "https://github.com/o/a/pull/1" in out.note
    assert "https://github.com/o/b/pull/2" in out.note

    merge_md = (ctx.service.workspace(t).artifacts_dir / "merge.md").read_text(
        encoding="utf-8"
    )
    assert merge_md.startswith("# Merge (multi-repo)")
    assert "https://github.com/o/a/pull/1" in merge_md
    assert "https://github.com/o/b/pull/2" in merge_md


def test_multi_repo_url_fallback_partial_merge_stays_same_state(tmp_path, monkeypatch):
    """One repo's branch-keyed ``pr_status`` is ``None`` but the URL-keyed
    fallback reports it merged; the other is open with green CI →
    same-state no-op (no premature DONE)."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            # repo-a: head branch gone → None (falls back to URL lookup).
            remote_a: None,
            # repo-b: still open + mergeable.
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
            },
        },
        pr_by_url_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
        },
        ci_responses={
            remote_b: {"conclusion": "success", "failing": []},
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    # repo-a merged (via fallback), repo-b green but not eligible →
    # surface HUMAN_MR_APPROVAL so a human can merge the remaining PR.
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert not (ctx.service.workspace(t).artifacts_dir / "merge.md").exists()


def test_multi_repo_partial_merge_stays_same_state(tmp_path, monkeypatch):
    """One PR merged, one PR open with green CI → same-state no-op."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_b: {"conclusion": "success", "failing": []},
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    # repo-a merged, repo-b green but not eligible → surface
    # HUMAN_MR_APPROVAL so a human can merge the remaining PR.
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert not (ctx.service.workspace(t).artifacts_dir / "merge.md").exists()


def test_multi_repo_one_pr_closed_unmerged_blocks(tmp_path, monkeypatch):
    """One PR merged, one PR closed-unmerged → BLOCKED with repo_id + url."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": False,
                "state": "closed",
                "url": "https://github.com/o/b/pull/2",
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "repo-b" in out.note
    assert "https://github.com/o/b/pull/2" in out.note


def test_multi_repo_conflicting_with_clone_runs_rebase(tmp_path, monkeypatch):
    """A conflicting repo WITH a workspace clone runs the rebase agent on that
    repo's clone, force-pushes the rebased branch to the per-repo remote, and
    re-polls (same state) — the multi-repo rebase auto-recovery."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {"merged": True, "state": "closed", "url": "u-a"},
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": False,
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    captured = {}
    from robotsix_mill.stages import merge as merge_mod

    def fake_rebase(
        *, settings, repo_dir, branch, target, memory, remote_url=None, token=None
    ):
        captured["repo_dir"] = repo_dir
        captured["branch"] = branch
        captured["target"] = target

        class _R:
            status = "DONE"
            updated_memory = ""

        return _R()

    monkeypatch.setattr(merge_mod, "run_rebase_agent", fake_rebase)
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # Reconcile is exercised separately; this test targets the
    # rebase/ci-fix flow, so treat the PR branch as in sync.
    monkeypatch.setattr(
        merge_mod.git_ops,
        "reconcile_with_remote_pr",
        lambda *a, **k: merge_mod.git_ops.ReconcileResult.SYNCED,
    )
    monkeypatch.setattr(merge_mod.git_ops, "fetch", lambda *a, **k: None)
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {}
    monkeypatch.setattr(
        merge_mod.git_ops,
        "post_push_check",
        lambda repo, branch, target, remote_url, token: (
            pushed.update({"branch": branch, "remote": remote_url})
            or PostPushResult.PASS
        ),
    )

    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert captured["repo_dir"].endswith("repos/repo-b")
    assert captured["branch"] == branch
    assert pushed["branch"] == branch
    assert pushed["remote"] == remote_b
    counter = ctx.service.workspace(t).artifacts_dir / "rebase_repo-b.count"
    assert merge_mod._read_counter(counter) == 0


def test_multi_repo_conflicting_without_clone_blocks(tmp_path, monkeypatch):
    """A conflicting repo whose clone is missing → BLOCKED naming the repo."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {"merged": True, "state": "closed", "url": "u-a"},
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": False,
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # No clone materialised for repo-b.
    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "clone for repo-b missing — re-run implement" in out.note


def test_multi_repo_rebase_attempt_cap_blocks(tmp_path, monkeypatch):
    """Exhausting the per-repo rebase attempt counter → BLOCKED naming the
    repo + attempt count, and resets the counter for a future resume."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {"merged": True, "state": "closed", "url": "u-a"},
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": False,
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    from robotsix_mill.stages import merge as merge_mod

    # Pre-seed the counter at the cap so the next attempt exceeds it.
    counter = ctx.service.workspace(t).artifacts_dir / "rebase_repo-b.count"
    merge_mod._write_counter(counter, ctx.settings.rebase_max_attempts)

    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "repo-b" in out.note
    assert "attempt" in out.note
    assert merge_mod._read_counter(counter) == 0


def test_multi_repo_one_pr_failing_ci_missing_clone_blocks(tmp_path, monkeypatch):
    """One PR green, one PR open + CI failure → the aggregator routes the
    failing repo to the inline CI-fix path; with no workspace clone it
    BLOCKS asking for re-implement (rather than the old immediate block)."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/a/pull/1",
                "mergeable": True,
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_a: {"conclusion": "success", "failing": []},
            remote_b: {"conclusion": "failure", "failing": []},
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "repo-b" in out.note
    assert "missing" in out.note and "re-run implement" in out.note


def test_multi_repo_failing_ci_with_clone_runs_ci_fix(tmp_path, monkeypatch):
    """A failing-CI repo WITH a workspace clone runs the CI-fix agent on that
    repo's clone, pushes the fix, and re-polls (same state) — the multi-repo
    auto-recovery the d776 follow-up wires."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {"merged": True, "state": "closed", "url": "u-a"},
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
                "sha": "deadbeef",
            },
        },
        ci_responses={
            remote_b: {"conclusion": "failure", "failing": [{"name": "tests"}]},
        },
    )
    # Best-effort log enrichment must not require real network.
    monkeypatch.setattr(
        github.GitHubForge, "list_workflow_runs", lambda self, *, head_sha: []
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # Materialise repo-b's clone under repos/<id> so the fix path proceeds.
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    captured = {}
    from robotsix_mill.stages import merge as merge_mod

    def fake_ci_fix(*, settings, repo_dir, branch, failing_summary, **kw):
        captured["repo_dir"] = repo_dir
        captured["branch"] = branch

        class _R:
            status = "DONE"
            updated_memory = ""

        return _R()

    monkeypatch.setattr(merge_mod, "run_ci_fix_agent", fake_ci_fix)
    # Keep the test hermetic: don't open a real Langfuse-exporting span.
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # New commits present → push path taken.
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {}
    monkeypatch.setattr(
        merge_mod.git_ops,
        "post_push_check",
        lambda repo, branch, target, remote_url, token: (
            pushed.update({"branch": branch, "remote": remote_url})
            or PostPushResult.PASS
        ),
    )

    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    # Stays in IMPLEMENT_COMPLETE to re-poll after the fix push.
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert captured["repo_dir"].endswith("repos/repo-b")
    assert pushed["remote"] == remote_b
    # Attempt counter reset on a productive push.
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_repo-b.count"
    assert merge_mod._read_counter(counter) == 0
    # The inline cross-repo loop leaves a per-attempt breadcrumb in history
    # (it never transitions to FIXING_CI, so without this the trail is empty).
    notes = [e.note or "" for e in ctx.service.history(t.id)]
    assert any(
        "ci_fix (cross-repo) attempt 1/" in n and "repo-b" in n and "tests" in n
        for n in notes
    ), notes


def test_multi_repo_ci_fix_cycle_ceiling_blocks(tmp_path, monkeypatch):
    """After ci_fix_max_cycles cycles of the agent reporting DONE + producing
    commits while CI stays red, the next call triggers BLOCKED without
    invoking the agent."""
    ctx = _gh(tmp_path, ci_fix_max_cycles="3")
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "https://github.com/o/b/pull/2",
                "mergeable": True,
                "sha": "deadbeef",
            },
        },
        ci_responses={
            # check_status in _run_multi_repo returns failure (routes to ci_fix).
            # But _multi_repo_fix_ci also calls check_status — we need
            # consistent failure there too.
            remote_b: {"conclusion": "failure", "failing": [{"name": "tests"}]},
        },
    )
    monkeypatch.setattr(
        github.GitHubForge, "list_workflow_runs", lambda self, *, head_sha: []
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # Materialise repo-b clone.
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    from robotsix_mill.stages import merge as merge_mod
    from robotsix_mill.agents.ci_fixing import CiFixResult

    agent_calls = {"n": 0}

    def fake_agent(**kw):
        agent_calls["n"] += 1
        return CiFixResult(status="DONE", summary="ok")

    monkeypatch.setattr(merge_mod, "run_ci_fix_agent", fake_agent)
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # Simulate a fresh churn commit every cycle: local != remote.
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    monkeypatch.setattr(
        merge_mod.git_ops,
        "post_push_check",
        lambda *a, **k: PostPushResult.PASS,
    )

    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_repo-b_cycles.txt"

    # Cycles 1-3 run the agent → IMPLEMENT_COMPLETE.
    for expected in (1, 2, 3):
        out = MergeStage().run(t, ctx)
        assert out.next_state is State.IMPLEMENT_COMPLETE
        assert merge_mod._read_counter(cycle_path) == expected
    assert agent_calls["n"] == 3

    # Cycle 4 reaches the ceiling → BLOCKED without running the agent.
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "hard ceiling of 3 cycle(s)" in out.note
    # Agent NOT invoked on the blocking cycle.
    assert agent_calls["n"] == 3
    # Cycle counter reset to 0 on the blocking return.
    assert merge_mod._read_counter(cycle_path) == 0


def test_multi_repo_ci_fix_cycle_reset_on_green(tmp_path, monkeypatch):
    """Two failing cycles bump the per-repo cycle counter; then CI turns green
    → counter resets to 0."""
    ctx = _gh(tmp_path, ci_fix_max_cycles="8")
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    # Mutable dict so we can flip the CI conclusion between calls.
    ci_state = {"conclusion": "failure", "failing": [{"name": "tests"}]}

    def fake_pr_status(self, *, source_branch):
        return {
            "merged": False,
            "state": "open",
            "url": "https://github.com/o/b/pull/2",
            "mergeable": True,
            "sha": "deadbeef",
        }

    def fake_check_status(self, *, source_branch):
        return dict(ci_state)

    monkeypatch.setattr(github.GitHubForge, "pr_status", fake_pr_status)
    monkeypatch.setattr(github.GitHubForge, "check_status", fake_check_status)
    monkeypatch.setattr(
        github.GitHubForge, "list_workflow_runs", lambda self, *, head_sha: []
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    repo_b_dir = ctx.service.workspace(t).dir / "repos" / "repo-b"
    (repo_b_dir / ".git").mkdir(parents=True)

    from robotsix_mill.stages import merge as merge_mod
    from robotsix_mill.agents.ci_fixing import CiFixResult

    monkeypatch.setattr(
        merge_mod,
        "run_ci_fix_agent",
        lambda **kw: CiFixResult(status="DONE", summary="ok"),
    )
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    monkeypatch.setattr(
        merge_mod.git_ops, "post_push_check", lambda *a, **k: PostPushResult.PASS
    )

    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    cycle_path = ctx.service.workspace(t).artifacts_dir / "ci_fix_repo-b_cycles.txt"

    # Two failing cycles bump the counter.
    MergeStage().run(t, ctx)
    MergeStage().run(t, ctx)
    assert merge_mod._read_counter(cycle_path) == 2

    # CI turns green — the _run_multi_repo poll observes success and
    # resets the counter.  With no eligible review the all-green hold now
    # surfaces HUMAN_MR_APPROVAL so a human can merge.
    ci_state["conclusion"] = "success"
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.HUMAN_MR_APPROVAL
    assert merge_mod._read_counter(cycle_path) == 0


def test_multi_repo_all_green_auto_merges_when_eligible(tmp_path, monkeypatch):
    """All PRs green + review marks auto-merge eligible → each green PR is
    merged via its own forge; stays same-state so the next poll sees DONE."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "u-a",
                "mergeable": True,
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "u-b",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_a: {"conclusion": "success", "failing": []},
            remote_b: {"conclusion": "success", "failing": []},
        },
    )
    merged_calls = []

    def _fake_merge(self, *, source_branch):
        merged_calls.append(self._remote_url)
        return {"merged": True}

    monkeypatch.setattr(github.GitHubForge, "merge_pr", _fake_merge)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # Review artifact marking auto-merge eligible.
    ctx.service.workspace(t).artifacts_dir.joinpath("review.md").write_text(
        "verdict: APPROVE\nauto_merge_eligible: true\n", encoding="utf-8"
    )
    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {"repo_id": "repo-b", "branch": branch, "url": "u-b"},
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE  # re-poll → next sees DONE
    assert sorted(merged_calls) == [remote_a, remote_b]


def test_multi_repo_all_green_auto_merges_without_artifact(tmp_path, monkeypatch):
    """All PRs green — auto-merge fires without a review artifact
    (artifact check removed)."""
    ctx = _gh(tmp_path, auto_merge_enabled="true", review_enabled="true")
    remote_a = "https://github.com/o/a.git"
    _install_multirepo_registry([("repo-a", remote_a)])
    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "u-a",
                "mergeable": True,
            },
        },
        ci_responses={remote_a: {"conclusion": "success", "failing": []}},
    )
    merged_calls = []

    def _fake_merge(self, *, source_branch):
        merged_calls.append(1)
        return {"merged": True}

    monkeypatch.setattr(github.GitHubForge, "merge_pr", _fake_merge)
    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # No review.md — was previously not eligible, now auto-merges.
    _write_pr_urls(ctx, t, [{"repo_id": "repo-a", "branch": branch, "url": "u-a"}])

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE  # re-poll → next sees DONE
    assert merged_calls == [1]


def test_multi_repo_unknown_repo_id_blocks(tmp_path, monkeypatch):
    """A repo_id not in ReposRegistry → BLOCKED with 'unknown repo_id'."""
    ctx = _gh(tmp_path)
    _install_multirepo_registry(
        [("repo-a", "https://github.com/o/a.git")],
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "ghost-repo",
                "branch": branch,
                "url": "https://github.com/o/ghost/pull/9",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "unknown repo_id" in out.note


def test_multi_repo_corrupt_pr_urls_blocks(tmp_path):
    """Invalid JSON in pr_urls.json → BLOCKED with 'corrupted'."""
    ctx = _gh(tmp_path)
    t = _make_meta_ticket(ctx)
    ws = ctx.service.workspace(t)
    (ws.artifacts_dir / "pr_urls.json").write_text("{not valid json", encoding="utf-8")

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "corrupted" in out.note


def test_multi_repo_per_repo_forge_called_with_correct_remote(tmp_path, monkeypatch):
    """pr_status is invoked once per repo with that repo's _remote_url."""
    ctx = _gh(tmp_path)
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    seen_pr, _ = _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/a/pull/1",
            },
            remote_b: {
                "merged": True,
                "state": "closed",
                "url": "https://github.com/o/b/pull/2",
            },
        },
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "repo_id": "repo-a",
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
            {
                "repo_id": "repo-b",
                "branch": branch,
                "url": "https://github.com/o/b/pull/2",
            },
        ],
    )

    MergeStage().run(t, ctx)
    assert len(seen_pr) == 2
    remotes_called = {entry["remote"] for entry in seen_pr}
    assert remotes_called == {remote_a, remote_b}


def test_single_repo_unchanged_when_no_pr_urls_json(tmp_path, monkeypatch):
    """When pr_urls.json is absent, the existing single-repo dispatch
    runs and ``merge.md`` is the byte-identical single-line shape."""
    ctx = _gh(tmp_path)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "https://github.com/o/r/pull/77",
        },
    )

    t = _human_mr_approval(ctx)
    # Sanity: pr_urls.json must NOT exist
    assert not (ctx.service.workspace(t).artifacts_dir / "pr_urls.json").exists()
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    merge_md = (ctx.service.workspace(t).artifacts_dir / "merge.md").read_text(
        encoding="utf-8"
    )
    assert merge_md == "merged: https://github.com/o/r/pull/77\n"


def test_multi_repo_entry_missing_repo_id_blocks(tmp_path):
    """A malformed ``pr_urls.json`` entry (missing/empty/non-string
    ``repo_id``) must NOT bubble a ``KeyError`` past the caller's narrow
    ``except ConfigError`` arm — it must BLOCK cleanly.

    Pins ``_repo_config_for_entry`` raising ``ConfigError`` for the
    missing / empty / non-string-``repo_id`` cases so the aggregator's
    existing arm catches it."""
    ctx = _gh(tmp_path)
    _install_multirepo_registry([("repo-a", "https://github.com/o/a.git")])

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    # Entry has no ``repo_id`` key at all.
    _write_pr_urls(
        ctx,
        t,
        [
            {
                "branch": branch,
                "url": "https://github.com/o/a/pull/1",
            },
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert "unknown repo_id" in out.note


# ============================================================
# Branch cleanup on DONE-via-merge (delete_branch_on_merge)
# ============================================================


def _merged_pr_status(monkeypatch):
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": True,
            "state": "closed",
            "url": "https://gh/o/r/pull/3",
        },
    )


def test_done_via_merge_deletes_branch_when_flag_enabled(tmp_path, monkeypatch):
    """delete_branch_on_merge=True → delete_branch called once with the branch."""
    ctx = _gh(tmp_path, delete_branch_on_merge=True)
    _merged_pr_status(monkeypatch)
    calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "delete_branch",
        lambda self, *, branch: calls.append(branch) or True,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert calls == [f"mill/{t.id}"]


def test_done_via_merge_skips_delete_when_flag_disabled(tmp_path, monkeypatch):
    """delete_branch_on_merge=False → delete_branch never called."""
    ctx = _gh(tmp_path, delete_branch_on_merge=False)
    _merged_pr_status(monkeypatch)
    calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "delete_branch",
        lambda self, *, branch: calls.append(branch) or True,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE
    assert calls == []


def test_done_via_merge_cleanup_failure_does_not_block_done(tmp_path, monkeypatch):
    """A delete_branch that raises/returns False must not prevent DONE."""
    ctx = _gh(tmp_path, delete_branch_on_merge=True)
    _merged_pr_status(monkeypatch)

    def boom(self, *, branch):
        raise RuntimeError("forge down")

    monkeypatch.setattr(github.GitHubForge, "delete_branch", boom)
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.DONE


def test_blocked_closed_unmerged_does_not_delete_branch(tmp_path, monkeypatch):
    """A BLOCKED/PR-closed transition must not trigger branch deletion."""
    ctx = _gh(tmp_path, delete_branch_on_merge=True)
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {
            "merged": False,
            "state": "closed",
            "url": "u",
        },
    )
    calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "delete_branch",
        lambda self, *, branch: calls.append(branch) or True,
    )
    t = _implement_complete(ctx)
    out = MergeStage().run(t, ctx)
    assert out.next_state is State.BLOCKED
    assert calls == []


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


def test_multi_repo_fix_ci_diverged_returns_blocked_and_skips_push(
    tmp_path, monkeypatch
):
    """When reconcile reports the PR branch DIVERGED, _multi_repo_fix_ci must
    BLOCK and must NOT call post_push_check — the lease cannot protect a case
    where reconcile already fetched the foreign commit into the lease ref."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "tests"}],
        },
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
        "pr_status",
        lambda self, *, source_branch: {"sha": ""},
    )
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    # Diverged reconcile must short-circuit before any agent/push.
    monkeypatch.setattr(
        merge_mod.git_ops,
        "reconcile_with_remote_pr",
        lambda *a, **k: ReconcileResult.DIVERGED,
    )
    # If the guard were removed, the agent would run + produce a commit
    # (head != remote) → the push below would fire.
    monkeypatch.setattr(
        merge_mod,
        "run_ci_fix_agent",
        lambda **k: type("_R", (), {"status": "DONE", "updated_memory": ""})(),
    )
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {"called": False}

    def _spy_push(*a, **k):
        pushed["called"] = True
        raise AssertionError("post_push_check must not run on a diverged branch")

    monkeypatch.setattr(merge_mod.git_ops, "post_push_check", _spy_push)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    (ctx.service.workspace(t).dir / "repos" / "repo-b" / ".git").mkdir(parents=True)

    out = MergeStage()._multi_repo_fix_ci(
        t, ctx, {"repo_id": "repo-b", "branch": branch, "url": "u"}
    )
    assert out.next_state is State.BLOCKED
    assert pushed["called"] is False
    assert "diverged" in (out.note or "").lower()


def test_multi_repo_fix_ci_failed_attempt_records_history_note(tmp_path, monkeypatch):
    """A cross-repo ci-fix attempt whose agent fails must leave a per-attempt
    breadcrumb in ticket history.

    The inline multi-repo loop never transitions to FIXING_CI, so a failed
    attempt returns Outcome(IMPLEMENT_COMPLETE) with no transition row. Without
    an explicit history note, a ticket that later BLOCKs "failed after N
    attempt(s)" shows zero fixing_ci rows in /history — the mystery this fix
    closes."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

    monkeypatch.setattr(
        github.GitHubForge,
        "check_status",
        lambda self, *, source_branch: {
            "conclusion": "failure",
            "failing": [{"name": "tests"}],
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "list_code_scanning_alerts",
        lambda self, *, source_branch: [],
    )
    monkeypatch.setattr(
        github.GitHubForge, "pr_files", lambda self, *, source_branch: []
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_status",
        lambda self, *, source_branch: {"sha": ""},
    )
    monkeypatch.setattr(
        merge_mod.tracing,
        "start_ticket_root_span",
        lambda *a, **k: contextlib.nullcontext(),
    )
    monkeypatch.setattr(
        merge_mod.git_ops, "reconcile_with_remote_pr", lambda *a, **k: None
    )
    # Agent fails (status != DONE) → failed-attempt re-poll branch.
    monkeypatch.setattr(
        merge_mod,
        "run_ci_fix_agent",
        lambda **k: type("_R", (), {"status": "ERROR", "updated_memory": ""})(),
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    (ctx.service.workspace(t).dir / "repos" / "repo-b" / ".git").mkdir(parents=True)

    out = MergeStage()._multi_repo_fix_ci(
        t, ctx, {"repo_id": "repo-b", "branch": branch, "url": "u"}
    )
    # Failed attempt re-polls (no transition) but advances the counter…
    assert out.next_state is State.IMPLEMENT_COMPLETE
    counter = ctx.service.workspace(t).artifacts_dir / "ci_fix_repo-b.count"
    assert merge_mod._read_counter(counter) == 1
    # …and leaves both a start and a failure breadcrumb in history.
    notes = [e.note or "" for e in ctx.service.history(t.id)]
    assert any("ci_fix (cross-repo) attempt 1/" in n and "repo-b" in n for n in notes)
    assert any("failed (agent error)" in n for n in notes), notes


def test_multi_repo_rebase_diverged_returns_blocked_and_skips_push(
    tmp_path, monkeypatch
):
    """When reconcile reports the PR branch DIVERGED, _multi_repo_rebase must
    BLOCK and must NOT call post_push_check."""
    from robotsix_mill.stages import merge as merge_mod

    ctx = _gh(tmp_path)
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-b", remote_b)])

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
    monkeypatch.setattr(merge_mod.git_ops, "head_sha", lambda d: "newsha")
    monkeypatch.setattr(merge_mod.git_ops, "remote_branch_sha", lambda d, b: "oldsha")
    pushed = {"called": False}

    def _spy_push(*a, **k):
        pushed["called"] = True
        raise AssertionError("post_push_check must not run on a diverged branch")

    monkeypatch.setattr(merge_mod.git_ops, "post_push_check", _spy_push)

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    (ctx.service.workspace(t).dir / "repos" / "repo-b" / ".git").mkdir(parents=True)

    out = MergeStage()._multi_repo_rebase(
        t, ctx, {"repo_id": "repo-b", "branch": branch, "url": "u"}
    )
    assert out.next_state is State.BLOCKED
    assert pushed["called"] is False
    assert "diverged" in (out.note or "").lower()


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


def test_multi_repo_one_repo_changes_requested_routes_to_addressing_review(
    tmp_path, monkeypatch
):
    """Multi-repo: all green + eligible, but one repo reports CHANGES_REQUESTED
    with comments → ADDRESSING_REVIEW; no repo's merge_pr is called."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "u-a",
                "mergeable": True,
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "u-b",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_a: {"conclusion": "success", "failing": []},
            remote_b: {"conclusion": "success", "failing": []},
        },
    )
    review_by_remote = {
        remote_a: None,
        remote_b: {
            "state": "CHANGES_REQUESTED",
            "comments": [{"body": "fix", "path": "b.py", "line": 2}],
            "files": ["b.py"],
        },
    }
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: review_by_remote.get(self._remote_url),
    )
    merged_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: (
            merged_calls.append(self._remote_url) or {"merged": True}
        ),
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ctx.service.workspace(t).artifacts_dir.joinpath("review.md").write_text(
        "verdict: APPROVE\nauto_merge_eligible: true\n", encoding="utf-8"
    )
    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {"repo_id": "repo-b", "branch": branch, "url": "u-b"},
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.ADDRESSING_REVIEW
    assert merged_calls == []
    assert (ctx.service.workspace(t).artifacts_dir / "review_feedback.json").exists()


def test_multi_repo_no_changes_requested_auto_merges(tmp_path, monkeypatch):
    """Multi-repo: all green + eligible and no repo requests changes →
    auto-merge proceeds as today (regression guard)."""
    ctx = _gh(
        tmp_path,
        auto_merge_enabled="true",
        review_enabled="true",
        review_feedback_enabled="true",
    )
    remote_a = "https://github.com/o/a.git"
    remote_b = "https://github.com/o/b.git"
    _install_multirepo_registry([("repo-a", remote_a), ("repo-b", remote_b)])

    _route_by_remote(
        monkeypatch,
        pr_responses={
            remote_a: {
                "merged": False,
                "state": "open",
                "url": "u-a",
                "mergeable": True,
            },
            remote_b: {
                "merged": False,
                "state": "open",
                "url": "u-b",
                "mergeable": True,
            },
        },
        ci_responses={
            remote_a: {"conclusion": "success", "failing": []},
            remote_b: {"conclusion": "success", "failing": []},
        },
    )
    monkeypatch.setattr(
        github.GitHubForge,
        "pr_review_status",
        lambda self, *, source_branch: None,
    )
    merged_calls = []
    monkeypatch.setattr(
        github.GitHubForge,
        "merge_pr",
        lambda self, *, source_branch: (
            merged_calls.append(self._remote_url) or {"merged": True}
        ),
    )

    t = _make_meta_ticket(ctx)
    branch = f"mill/{t.id}"
    ctx.service.workspace(t).artifacts_dir.joinpath("review.md").write_text(
        "verdict: APPROVE\nauto_merge_eligible: true\n", encoding="utf-8"
    )
    _write_pr_urls(
        ctx,
        t,
        [
            {"repo_id": "repo-a", "branch": branch, "url": "u-a"},
            {"repo_id": "repo-b", "branch": branch, "url": "u-b"},
        ],
    )

    out = MergeStage().run(t, ctx)
    assert out.next_state is State.IMPLEMENT_COMPLETE
    assert sorted(merged_calls) == [remote_a, remote_b]


# ============================================================
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
